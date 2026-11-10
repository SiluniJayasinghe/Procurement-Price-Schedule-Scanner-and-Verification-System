"""Price Schedule Scanner: multimodal OCR assisted with exact Decimal arithmetic."""
import base64
import io
import json
import os
import re
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from typing import Any

import fitz
import httpx
from dotenv import load_dotenv
from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from PIL import Image, ImageOps
from pydantic import BaseModel

load_dotenv()
app = FastAPI(title="Price Schedule Scanner", version="1.0.0")
origins = os.getenv("CORS_ORIGINS", "http://localhost:5173,http://127.0.0.1:5173").split(",")
app.add_middleware(CORSMiddleware, allow_origins=[s.strip() for s in origins], allow_credentials=False, allow_methods=["*"], allow_headers=["*"])
MAX_FILE = 16 * 1024 * 1024
MAX_PAGES = 12
MODEL = os.getenv("GEMINI_MODEL", "gemini-2.5-flash")
MONEY = Decimal("0.01")

class CheckRequest(BaseModel):
    pages: list[dict[str, Any]]
    vat_multiplier: Any = None
    vat_multipliers: Any = None

def decimal_or_none(value):
    if value is None or isinstance(value, bool):
        return None
    cleaned = re.sub(r"[^0-9.\-]", "", str(value).replace(",", ""))
    if not cleaned or cleaned in ("-", ".", "-."):
        return None
    try:
        result = Decimal(cleaned)
        return result if result.is_finite() else None
    except InvalidOperation:
        return None

def price_per_unit(value):
    if not isinstance(value, str):
        return decimal_or_none(value)
    numbers = re.findall(r"\d+(?:,\d{3})*(?:\.\d+)?", value)
    if re.search(r"\bper\b|\bfor\b", value, re.IGNORECASE) and len(numbers) >= 2:
        amount = decimal_or_none(numbers[0])
        units = decimal_or_none(numbers[1])
        if amount is not None and units is not None and units > 0:
            return amount / units
    return decimal_or_none(value)

def required_quantity(row):
    quantity = decimal_or_none(row.get("quantity"))
    if quantity is not None:
        return quantity
    for key in ("quantity_text", "description", "item", "raw_text"):
        text = row.get(key)
        if isinstance(text, str):
            match = re.search(r"\brequired\b\s*[:\-]?\s*(\d+(?:,\d{3})*(?:\.\d+)?)", text, re.IGNORECASE)
            if match:
                return decimal_or_none(match.group(1))
    return None

def integer_quantity(value):
    parsed = decimal_or_none(value)
    if parsed is None or parsed != parsed.to_integral_value():
        return None
    return parsed.to_integral_value()

def selected_quote(row, price_key):
    quotes = row.get("quote_options")
    if not isinstance(quotes, list) or not quotes:
        return None
    candidates = []
    direct_value = row.get(price_key)
    if direct_value is not None:
        unit = price_per_unit(direct_value)
        if unit is not None and unit >= 0:
            candidates.append((unit, -1))
    else:
        bulk_key = "bulk_price_with_vat" if price_key == "unit_price_with_vat" else "bulk_price_without_vat"
        bulk_amount = decimal_or_none(row.get(bulk_key))
        bulk_quantity = decimal_or_none(row.get("bulk_quantity"))
        if bulk_amount is not None and bulk_quantity is not None and bulk_quantity > 0:
            candidates.append((bulk_amount / bulk_quantity, -1))
    for index, quote in enumerate(quotes):
        if not isinstance(quote, dict):
            continue
        explicit_unit = quote.get(price_key)
        if explicit_unit is None:
            explicit_unit = quote.get("unit_price")
        bulk_key = "bulk_price_with_vat" if price_key == "unit_price_with_vat" else "bulk_price_without_vat"
        value = explicit_unit
        if value is None:
            value = next((quote.get(key) for key in ("price", "amount", bulk_key) if quote.get(key) is not None), None)
        unit = price_per_unit(explicit_unit) if explicit_unit is not None else price_per_unit(value)
        quote_quantity = decimal_or_none(quote.get("bulk_quantity", quote.get("for_quantity", quote.get("per"))))
        amount = decimal_or_none(value)
        if explicit_unit is None and amount is not None and quote_quantity is not None and quote_quantity > 0 and not (isinstance(value, str) and re.search(r"\bper\b|\bfor\b", value, re.IGNORECASE)):
            unit = amount / quote_quantity
        if unit is not None and unit >= 0:
            candidates.append((unit, index))
    return min(candidates) if candidates else None

def pricing_details(row, factor):
    options = row.get("quote_options")
    candidates = options if isinstance(options, list) else []
    candidates = [row, *[option for option in candidates if isinstance(option, dict)]]
    normalized = []
    for index, option in enumerate(candidates):
        bulk_quantity = decimal_or_none(option.get("bulk_quantity", option.get("for_quantity")))
        bulk_with_vat = decimal_or_none(option.get("bulk_price_with_vat"))
        bulk_without_vat = decimal_or_none(option.get("bulk_price_without_vat"))
        if bulk_with_vat is None and index == 0:
            bulk_with_vat = decimal_or_none(option.get("unit_price_with_vat"))
        if bulk_without_vat is None and index == 0:
            bulk_without_vat = decimal_or_none(option.get("unit_price_without_vat"))
        provided_bulk_with_vat = bulk_with_vat
        if bulk_without_vat is not None and factor is not None:
            bulk_with_vat = bulk_without_vat * factor
        if bulk_quantity is None or bulk_quantity <= 0 or bulk_with_vat is None or bulk_with_vat < 0:
            continue
        normalized.append((bulk_with_vat / bulk_quantity, index, bulk_quantity, bulk_with_vat, bulk_without_vat, provided_bulk_with_vat))
    return min(normalized, key=lambda value: value[0]) if normalized else None

def fmt(value):
    return str(value.quantize(MONEY, rounding=ROUND_HALF_UP)) if value is not None else None

def fmt_quantity(value):
    return str(value) if value is not None else None

def vat_factor(value):
    if value is None or isinstance(value, bool):
        return None
    rate = str(value).strip()
    if rate.endswith("%"):
        rate = rate[:-1].strip()
    if not re.fullmatch(r"(?:\d+(?:\.\d*)?|\.\d+)", rate):
        return None
    try:
        parsed = Decimal(rate)
    except InvalidOperation:
        return None
    if not parsed.is_finite() or parsed < 0:
        return None
    return Decimal("1") + parsed / Decimal("100")

def verify(pages, vat_multiplier=None, vat_multipliers=None):
    output = []
    overall = Decimal("0")
    all_page_complete = True
    factor = vat_factor(vat_multiplier)
    recorded_grand_totals = [
        (decimal_or_none(page.get("document_grand_total")), decimal_or_none(page.get("document_grand_total_without_vat")))
        for page in pages
    ]
    group_ranges = []
    group_start = 0
    for index, (recorded_with_vat, recorded_without_vat) in enumerate(recorded_grand_totals):
        if recorded_with_vat is not None or recorded_without_vat is not None:
            group_ranges.append((group_start, index))
            group_start = index + 1
    if group_start < len(pages):
        group_ranges.append((group_start, len(pages) - 1))
    page_group_indices = {
        page_index: group_index
        for group_index, (start, end) in enumerate(group_ranges)
        for page_index in range(start, end + 1)
    }

    def page_factor(page_index):
        if isinstance(vat_multipliers, dict):
            return vat_factor(vat_multipliers.get(str(page_group_indices.get(page_index))))
        return factor

    for pi, page in enumerate(pages):
        rows_out = []
        page_total = Decimal("0")
        complete = True
        current_factor = page_factor(pi)
        recorded_with_vat, recorded_without_vat = recorded_grand_totals[pi]
        # Compare against the supplied grand total in the same VAT basis.
        # A VAT factor is only a fallback when no grand-total basis was given.
        prefer_inclusive_total = recorded_with_vat is not None or recorded_without_vat is None
        for ri, row in enumerate(page.get("rows", [])):
            quantity = integer_quantity(required_quantity(row))
            written_unit_price = price_per_unit(row.get("unit_price_with_vat"))
            bulk_pricing = pricing_details(row, current_factor)
            inclusive_quote = selected_quote(row, "unit_price_with_vat")
            exclusive_quote = selected_quote(row, "unit_price_without_vat")
            unit_price_with_vat = price_per_unit(row.get("unit_price_with_vat"))
            unit_price_without_vat = price_per_unit(row.get("unit_price_without_vat"))
            selected_quote_index = None
            selected_quote_unit_price = None
            normalized_inclusive_price = isinstance(row.get("unit_price_with_vat"), str) and bool(re.search(r"\bper\b|\bfor\b", row.get("unit_price_with_vat"), re.IGNORECASE))
            normalized_exclusive_price = isinstance(row.get("unit_price_without_vat"), str) and bool(re.search(r"\bper\b|\bfor\b", row.get("unit_price_without_vat"), re.IGNORECASE))
            bulk_quantity = decimal_or_none(row.get("bulk_quantity"))
            bulk_price_with_vat = decimal_or_none(row.get("bulk_price_with_vat"))
            bulk_price_without_vat = decimal_or_none(row.get("bulk_price_without_vat"))
            provided_bulk_price_with_vat = bulk_price_with_vat
            calculated_exclusive_unit_price = False
            if bulk_pricing is not None:
                unit_price_with_vat, selected_quote_index, bulk_quantity, bulk_price_with_vat, bulk_price_without_vat, provided_bulk_price_with_vat = bulk_pricing
                unit_price_with_vat = bulk_price_with_vat / bulk_quantity
                if bulk_price_without_vat is not None:
                    unit_price_without_vat = bulk_price_without_vat / bulk_quantity
                    calculated_exclusive_unit_price = True
                selected_quote_unit_price = unit_price_with_vat
                normalized_inclusive_price = True
            if bulk_pricing is None and exclusive_quote is not None:
                unit_price_without_vat, selected_quote_index = exclusive_quote
                selected_quote_unit_price = unit_price_without_vat
                normalized_exclusive_price = True
                quote_options = row.get("quote_options")
                selected_option = None
                if (
                    isinstance(quote_options, list)
                    and selected_quote_index >= 0
                    and selected_quote_index < len(quote_options)
                ):
                    selected_option = quote_options[selected_quote_index]
                if isinstance(selected_option, dict):
                    unit_price_with_vat = price_per_unit(selected_option.get("unit_price_with_vat"))
                    if unit_price_with_vat is None:
                        selected_bulk = decimal_or_none(selected_option.get("bulk_price_with_vat"))
                        selected_bulk_quantity = decimal_or_none(selected_option.get("bulk_quantity"))
                        if selected_bulk is not None and selected_bulk_quantity is not None and selected_bulk_quantity > 0:
                            unit_price_with_vat = selected_bulk / selected_bulk_quantity
                    written_unit_price = unit_price_with_vat
                else:
                    unit_price_with_vat = None
                    written_unit_price = None
            elif bulk_pricing is None and inclusive_quote is not None:
                unit_price_with_vat, selected_quote_index = inclusive_quote
                selected_quote_unit_price = unit_price_with_vat
                normalized_inclusive_price = True
            if row.get("unit_price_with_vat_present") is False:
                unit_price_with_vat = None
                written_unit_price = None
            provided_with_vat = decimal_or_none(row.get("document_total"))
            provided_without_vat = decimal_or_none(row.get("document_total_without_vat"))
            valid_quantity = quantity is not None and quantity >= 0
            valid_inclusive_price = unit_price_with_vat is not None and unit_price_with_vat >= 0
            valid_exclusive_price = unit_price_without_vat is not None and unit_price_without_vat >= 0
            valid_inputs = valid_quantity and (valid_inclusive_price or valid_exclusive_price)
            derived_inclusive_price = unit_price_without_vat * current_factor if valid_exclusive_price and current_factor is not None else None
            inclusive_price_source = derived_inclusive_price if derived_inclusive_price is not None else unit_price_with_vat
            calculated_inclusive_price = inclusive_price_source.quantize(MONEY, rounding=ROUND_HALF_UP) if inclusive_price_source is not None else None
            calculated_exclusive_total = (quantity * unit_price_without_vat).quantize(MONEY, rounding=ROUND_HALF_UP) if valid_quantity and valid_exclusive_price else None
            inclusive_total_price = calculated_inclusive_price if calculated_inclusive_price is not None else unit_price_with_vat
            exclusive_total_for_vat = calculated_exclusive_total if calculated_exclusive_total is not None else provided_without_vat
            if exclusive_total_for_vat is not None and current_factor is not None:
                calculated_inclusive_total = (exclusive_total_for_vat * current_factor).quantize(MONEY, rounding=ROUND_HALF_UP)
            elif valid_quantity and inclusive_total_price is not None:
                calculated_inclusive_total = (quantity * inclusive_total_price).quantize(MONEY, rounding=ROUND_HALF_UP)
            else:
                calculated_inclusive_total = None
            bulk_price_status = "not_given"
            if bulk_price_with_vat is not None:
                if current_factor is None:
                    bulk_price_status = "written_total"
                elif provided_bulk_price_with_vat is None:
                    bulk_price_status = "calculated"
                else:
                    bulk_price_status = "correct" if abs(provided_bulk_price_with_vat - bulk_price_with_vat) <= MONEY else "incorrect"
            inclusive_unit_status = "written_total" if valid_inclusive_price else "not_given"
            displayed_inclusive_price = unit_price_with_vat
            if derived_inclusive_price is not None:
                provided_inclusive_unit = written_unit_price
                if not valid_inclusive_price and provided_bulk_price_with_vat is not None and bulk_quantity is not None and bulk_quantity > 0:
                    provided_inclusive_unit = provided_bulk_price_with_vat / bulk_quantity
                if provided_inclusive_unit is not None:
                    inclusive_unit_status = "correct" if abs(provided_inclusive_unit - derived_inclusive_price) <= MONEY else "incorrect"
                else:
                    inclusive_unit_status = "calculated"
                displayed_inclusive_price = calculated_inclusive_price
            inclusive_total_status = "not_given"
            if calculated_inclusive_total is not None:
                inclusive_total_status = "calculated" if provided_with_vat is None else "correct" if provided_with_vat.quantize(MONEY) == calculated_inclusive_total else "incorrect"
            elif provided_with_vat is not None:
                inclusive_total_status = "written_total"
            exclusive_total_status = "not_given"
            if calculated_exclusive_total is not None:
                exclusive_total_status = "calculated" if provided_without_vat is None else "correct" if provided_without_vat.quantize(MONEY) == calculated_exclusive_total else "incorrect"
            elif provided_without_vat is not None:
                exclusive_total_status = "written_total"
            if calculated_inclusive_total is not None and prefer_inclusive_total and (valid_inclusive_price or current_factor is not None):
                page_total += calculated_inclusive_total
                basis = "vat_inclusive"
                calculated_total = calculated_inclusive_total
                provided = provided_with_vat
                status = inclusive_total_status
            elif calculated_exclusive_total is not None:
                page_total += calculated_exclusive_total
                basis = "vat_exclusive"
                calculated_total = calculated_exclusive_total
                provided = provided_without_vat
                status = exclusive_total_status
            elif provided_without_vat is not None:
                page_total += provided_without_vat
                basis = "vat_exclusive"
                calculated_total = provided_without_vat
                provided = provided_without_vat
                status = "written_total"
            elif provided_with_vat is not None:
                page_total += provided_with_vat
                basis = "vat_inclusive"
                calculated_total = provided_with_vat
                provided = provided_with_vat
                status = "written_total"
            else:
                basis = None
                calculated_total = None
                provided = None
                status = "needs_review"
            if calculated_total is None:
                complete = False
            rows_out.append({**row, "row_index": ri,
                "quantity": fmt_quantity(quantity) if quantity is not None else row.get("quantity"),
                "unit_price_with_vat": fmt(displayed_inclusive_price) if displayed_inclusive_price is not None else None if row.get("unit_price_with_vat_present") is False else row.get("unit_price_with_vat"),
                "unit_price_without_vat": fmt(unit_price_without_vat) if valid_exclusive_price else row.get("unit_price_without_vat"),
                "bulk_quantity": fmt(bulk_quantity),
                "bulk_price_with_vat": fmt(bulk_price_with_vat),
                "bulk_price_without_vat": fmt(bulk_price_without_vat),
                "bulk_unit_price_with_vat": fmt(bulk_price_with_vat / bulk_quantity) if bulk_price_with_vat is not None and bulk_quantity is not None and bulk_quantity > 0 else None,
                "bulk_unit_price_with_vat_status": bulk_price_status,
                "bulk_price_with_vat_status": bulk_price_status,
                "bulk_price_without_vat_status": "written_total" if bulk_price_without_vat is not None else "not_given",
                "selected_quote_index": selected_quote_index,
                "selected_quote_unit_price": fmt(selected_quote_unit_price),
                "quote_selection_status": "calculated" if selected_quote_index is not None else "not_given",
                "calculated_unit_price_with_vat": fmt(calculated_inclusive_price) if derived_inclusive_price is not None else None,
                "vat_rate_applied": current_factor is not None,
                "unit_price_with_vat_status": inclusive_unit_status,
                "unit_price_without_vat_status": "calculated" if calculated_exclusive_unit_price else "written_total" if valid_exclusive_price else "not_given",
                "calculation_basis": basis,
                "calculated_total": fmt(calculated_total),
                "calculated_total_with_vat": fmt(calculated_inclusive_total),
                "calculated_total_without_vat": fmt(calculated_exclusive_total),
                "document_total_status": inclusive_total_status,
                "document_total_without_vat_status": exclusive_total_status,
                "difference": fmt(calculated_total-provided) if calculated_total is not None and provided is not None else None,
                "difference_with_vat": fmt(calculated_inclusive_total-provided_with_vat) if calculated_inclusive_total is not None and provided_with_vat is not None else None,
                "difference_without_vat": fmt(calculated_exclusive_total-provided_without_vat) if calculated_exclusive_total is not None and provided_without_vat is not None else None,
                "status": status})
        if not rows_out:
            complete = False
        has_complete_inclusive_totals = bool(rows_out) and all(
            row["calculated_total_with_vat"] is not None for row in rows_out
        )
        if has_complete_inclusive_totals and recorded_with_vat is None and recorded_without_vat is None:
            page_total = sum(
                (Decimal(row["calculated_total_with_vat"]) for row in rows_out),
                Decimal("0"),
            )
            complete = True
            for row in rows_out:
                row["calculation_basis"] = "vat_inclusive"
                row["calculated_total"] = row["calculated_total_with_vat"]
                row["status"] = row["document_total_status"]
                row["difference"] = row["difference_with_vat"]
        has_inclusive_basis = any(row["calculation_basis"] == "vat_inclusive" for row in rows_out)
        uses_inclusive_total = recorded_with_vat is not None or (
            recorded_without_vat is None
            and (has_complete_inclusive_totals or current_factor is not None or has_inclusive_basis)
        )
        recorded = recorded_with_vat if uses_inclusive_total else recorded_without_vat
        page_basis = "vat_inclusive" if uses_inclusive_total else "vat_exclusive" if recorded_without_vat is not None or any(row["calculation_basis"] == "vat_exclusive" for row in rows_out) else None
        page_status = "needs_review" if not complete else "calculated" if recorded is None else "correct" if recorded.quantize(MONEY) == page_total else "incorrect"
        if not complete:
            all_page_complete = False
        else:
            overall += page_total
        page_warnings = page.get("warnings") if isinstance(page.get("warnings"), list) else []
        if recorded is not None:
            page_warnings = [
                warning for warning in page_warnings
                if not (isinstance(warning, str) and ("no grand total" in warning.lower() or "grand total" in warning.lower() and "provided" in warning.lower()))
            ]
        output.append({**page, "page_index": pi, "warnings": page_warnings, "rows": rows_out, "calculation_basis": page_basis, "calculated_grand_total": fmt(page_total) if complete else None, "grand_total_difference": fmt(page_total-recorded) if complete and recorded is not None else None, "grand_total_status": page_status})

    group_results = []
    for group_index, (start, end) in enumerate(group_ranges):
        group_pages = output[start:end + 1]
        group_complete = all(page["calculated_grand_total"] is not None for page in group_pages)
        group_total = sum((Decimal(page["calculated_grand_total"]) for page in group_pages if page["calculated_grand_total"] is not None), Decimal("0"))
        recorded_with_vat, recorded_without_vat = recorded_grand_totals[end]
        group_rows = [row for page in group_pages for row in page["rows"]]
        has_inclusive_basis = any(row["calculation_basis"] == "vat_inclusive" for page in group_pages for row in page["rows"])
        has_complete_inclusive_totals = bool(group_rows) and all(
            row["calculated_total_with_vat"] is not None for row in group_rows
        )
        group_factor = page_factor(start)
        uses_inclusive_total = recorded_with_vat is not None or (
            recorded_without_vat is None
            and (has_complete_inclusive_totals or group_factor is not None or has_inclusive_basis)
        )
        recorded = recorded_with_vat if uses_inclusive_total else recorded_without_vat
        group_status = "needs_review" if not group_complete else "calculated" if recorded is None else "correct" if recorded.quantize(MONEY) == group_total else "incorrect"
        group_result = {"table_group_index": group_index, "table_group_spans_pages": end > start, "table_group_requires_vat": any(decimal_or_none(row.get("unit_price_without_vat")) is not None for row in group_rows), "table_group_overall_total": fmt(group_total) if group_complete else None, "table_group_document_grand_total": fmt(recorded), "table_group_grand_total_difference": fmt(group_total - recorded) if group_complete and recorded is not None else None, "table_group_grand_total_status": group_status, "table_group_calculation_basis": "vat_inclusive" if uses_inclusive_total else "vat_exclusive" if recorded_without_vat is not None else None, "table_group_row_counts": {"correct": sum(row["status"] == "correct" for row in group_rows), "incorrect": sum(row["status"] == "incorrect" for row in group_rows), "calculated": sum(row["status"] == "calculated" for row in group_rows), "written_total": sum(row["status"] == "written_total" for row in group_rows), "needs_review": sum(row["status"] == "needs_review" for row in group_rows)}}
        group_results.append(group_result)
        for page_index in range(start, end + 1):
            output[page_index].update(group_result)
            output[page_index]["table_group_final_page"] = page_index == end
            if end > start and page_index < end:
                output[page_index]["warnings"] = [
                    warning for warning in output[page_index].get("warnings", [])
                    if not (isinstance(warning, str) and "grand total" in warning.lower())
                ]
            if recorded is not None and page_index == end and end > start:
                output[page_index]["grand_total_difference"] = None
                output[page_index]["grand_total_status"] = "calculated"

    overall_total = group_results[0]["table_group_overall_total"] if len(group_results) == 1 else None
    overall_group = group_results[0] if len(group_results) == 1 else None
    return {"pages": output, "overall_total": overall_total, "vat_multiplier": fmt(factor) if factor is not None else None, "vat_multipliers": {str(index): fmt(vat_factor(value)) for index, value in (vat_multipliers or {}).items() if vat_factor(value) is not None} if isinstance(vat_multipliers, dict) else None, "overall_document_grand_total": overall_group["table_group_document_grand_total"] if overall_group else None, "overall_grand_total_difference": overall_group["table_group_grand_total_difference"] if overall_group else None, "overall_grand_total_status": overall_group["table_group_grand_total_status"] if overall_group else None, "overall_calculation_basis": overall_group["table_group_calculation_basis"] if overall_group else None, "table_groups": group_results, "complete": all_page_complete and bool(output)}

@app.get("/api/health")
def health():
    return {"ok": True, "ocr_configured": bool(os.getenv("GEMINI_API_KEY")), "model": MODEL}

@app.post("/api/calculate")
def calculate(data: CheckRequest):
    if len(data.pages) > MAX_PAGES:
        raise HTTPException(400, "Too many pages")
    if data.vat_multiplier not in (None, "") and vat_factor(data.vat_multiplier) is None:
        raise HTTPException(400, "VAT rate must be a non-negative percentage such as 18 or 18%.")
    if data.vat_multipliers is not None:
        if not isinstance(data.vat_multipliers, dict) or any(value not in (None, "") and vat_factor(value) is None for value in data.vat_multipliers.values()):
            raise HTTPException(400, "Each VAT rate must be a non-negative percentage such as 18 or 18%.")
    return verify(data.pages, data.vat_multiplier, data.vat_multipliers)

def convert_to_images(blob: bytes, filename: str):
    images = []
    if filename.lower().endswith(".pdf"):
        try:
            pdf = fitz.open(stream=blob, filetype="pdf")
            if len(pdf) > MAX_PAGES:
                raise HTTPException(400, f"Maximum {MAX_PAGES} pages per PDF")
            for page in pdf:
                pix = page.get_pixmap(matrix=fitz.Matrix(1.7, 1.7), alpha=False)
                images.append(pix.tobytes("jpeg"))
            pdf.close()
        except HTTPException:
            raise
        except Exception as exc:
            raise HTTPException(400, "Unable to read PDF (encrypted or corrupted?)") from exc
    else:
        try:
            im = Image.open(io.BytesIO(blob))
            im = ImageOps.exif_transpose(im).convert("RGB")
            im.thumbnail((2300, 2300))
            out = io.BytesIO()
            im.save(out, format="JPEG", quality=89)
            images.append(out.getvalue())
        except Exception as exc:
            raise HTTPException(400, "Unsupported or invalid image") from exc
    return images

PROMPT = """You extract financial tables from scanned Sinhala/English price schedules. Printed AND handwritten digits are important. Read carefully, including handwritten row totals and page grand totals. For each document row extract item/reference as 'item', numeric quantity as 'quantity', VAT-inclusive unit price as 'unit_price_with_vat', VAT-exclusive unit price as 'unit_price_without_vat', VAT-inclusive line total as 'document_total', and VAT-exclusive line total as 'document_total_without_vat'. Carefully read and understand every Sinhala or English column header, subheader, label, and note to determine whether each monetary column is VAT-inclusive or VAT-exclusive. Do not infer VAT status from column position, number size, arithmetic, or a fixed document layout; different documents may arrange these columns differently. Map each value according to the meaning of its own header, and if a header is unreadable or the VAT status is ambiguous, output null for that field and add a warning instead of swapping or guessing. If one item row has prices under multiple supplier/bidder columns, treat each supplier's offer as a separate quote_options entry. Preserve the supplier or manufacturer name in that quote's 'supplier' field when visible. Read each supplier's own VAT-exclusive unit price from its VAT-exclusive unit-price column; do not mistake a supplier's unit price for the row total, and do not combine prices from different suppliers. When quote_options are present, keep the row-level unit-price fields null rather than copying one supplier's offer into them. If a pricing is sold in a bulk/package amount, extract its package count as 'bulk_quantity', package amount including VAT as 'bulk_price_with_vat', and package amount excluding VAT as 'bulk_price_without_vat'. If the document gives a quote's unit price directly, preserve it as 'unit_price_with_vat' or 'unit_price_without_vat' inside that quote; do NOT divide a direct unit price by the row quantity. If the document says 'required N' or similar instead of using a quantity column, put N in 'quantity' and also preserve the source phrase in 'quantity_text'. If a row contains two or more quoted pricings, put every pricing in 'quote_options'. For each quote, use direct 'unit_price_with_vat'/'unit_price_without_vat' when printed; otherwise use 'bulk_quantity' with 'bulk_price_with_vat'/'bulk_price_without_vat' when a package total is printed. The verifier selects the lowest VAT-exclusive unit price when those quotes are available; otherwise it selects the lowest comparable VAT-inclusive unit price. For package quotes, compare package price divided by package quantity on the same VAT basis. It then calculates the row's total without VAT as item quantity times the selected VAT-exclusive unit price. Do not select or calculate OCR values yourself. Group nested pricing lines under the parent item. Extract VAT-inclusive and VAT-exclusive bottom-of-page grand totals as 'document_grand_total' and 'document_grand_total_without_vat'. Preserve visible written amounts even if mathematically wrong. NEVER fill blank printed fields or calculate OCR values yourself. If uncertain, output null for that value and add a short note in 'warnings'. Extract every item row; avoid summary/header rows. Return only strict JSON object of format {"rows":[{"item":"...","quantity":"50","quantity_text":null,"bulk_quantity":null,"bulk_price_with_vat":null,"bulk_price_without_vat":null,"unit_price_with_vat":null,"unit_price_without_vat":null,"document_total":null,"document_total_without_vat":null,"quote_options":[{"supplier":"Supplier A","unit_price_with_vat":"125.00","unit_price_without_vat":"100.00","bulk_quantity":null,"bulk_price_with_vat":null,"bulk_price_without_vat":null}],"warnings":[]}],"document_grand_total":"6625000.00","document_grand_total_without_vat":null,"warnings":[]}. Numeric strings contain no commas or currency symbols. A missing field must be null. No Markdown. OCR uncertainty should be flagged in warnings; do not invent unreadable digits. For every row, include boolean 'unit_price_with_vat_present': true only if the schedule visibly has a VAT-inclusive unit price and a value is printed for that item. If the schedule has only VAT-exclusive unit-price columns, set this to false and set unit_price_with_vat to null, even if a number appears elsewhere. Do not infer an inclusive amount from an exclusive amount."""

async def recognize_image(image_bytes: bytes, api_key: str):
    endpoint = f"https://generativelanguage.googleapis.com/v1beta/models/{MODEL}:generateContent"
    body = {"contents": [{"parts": [{"text": PROMPT}, {"inline_data": {"mime_type": "image/jpeg", "data": base64.b64encode(image_bytes).decode("ascii")}}]}], "generationConfig": {"temperature": 0, "responseMimeType": "application/json"}}
    try:
        async with httpx.AsyncClient(timeout=115) as client:
            response = await client.post(endpoint, params={"key": api_key}, json=body)
        if response.status_code != 200:
            raise HTTPException(502, "OCR provider rejected the request. Check API key, model, quota and billing. Detail: " + response.text[:300])
        result = response.json()
        candidates = result.get("candidates")
        if not isinstance(candidates, list) or not candidates:
            detail = result.get("error", {}).get("message") if isinstance(result.get("error"), dict) else None
            raise ValueError(detail or "The OCR model returned no candidates. It may have blocked the image or reached a temporary limit.")
        candidate = candidates[0]
        content = candidate.get("content") if isinstance(candidate, dict) else None
        parts = content.get("parts") if isinstance(content, dict) else None
        text = next((part.get("text") for part in parts or [] if isinstance(part, dict) and isinstance(part.get("text"), str) and part["text"].strip()), None)
        if not text:
            finish_reason = candidate.get("finishReason") if isinstance(candidate, dict) else None
            raise ValueError(f"The OCR model returned no readable JSON (finish reason: {finish_reason or 'unknown'}).")
        text = text.strip()
        if text.startswith("```"):
            text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text, flags=re.IGNORECASE | re.DOTALL).strip()
        parsed = json.loads(text)
        if not isinstance(parsed, dict) or not isinstance(parsed.get("rows"), list):
            raise ValueError("OCR returned an invalid table")
        clean_rows = []
        for row in parsed["rows"][:150]:
            if isinstance(row, dict):
                clean_rows.append({"item": str(row.get("item") or ""), "quantity": row.get("quantity"), "quantity_text": row.get("quantity_text"), "bulk_quantity": row.get("bulk_quantity"), "bulk_price_with_vat": row.get("bulk_price_with_vat"), "bulk_price_without_vat": row.get("bulk_price_without_vat"), "unit_price_with_vat": row.get("unit_price_with_vat"), "unit_price_with_vat_present": row.get("unit_price_with_vat_present") if isinstance(row.get("unit_price_with_vat_present"), bool) else None, "unit_price_without_vat": row.get("unit_price_without_vat"), "document_total": row.get("document_total"), "document_total_without_vat": row.get("document_total_without_vat"), "quote_options": row.get("quote_options") if isinstance(row.get("quote_options"), list) else [], "warnings": row.get("warnings") if isinstance(row.get("warnings"), list) else []})
        return {"rows": clean_rows, "document_grand_total": parsed.get("document_grand_total"), "document_grand_total_without_vat": parsed.get("document_grand_total_without_vat"), "warnings": parsed.get("warnings") if isinstance(parsed.get("warnings"), list) else []}
    except HTTPException:
        raise
    except (httpx.HTTPError, ValueError, json.JSONDecodeError, KeyError, IndexError, TypeError) as exc:
        detail = str(exc).strip() or "The OCR response was empty or not valid JSON."
        raise HTTPException(502, "OCR failed or returned invalid data: " + detail[:220]) from exc

@app.post("/api/scan")
async def scan(files: list[UploadFile] = File(...)):
    api_key = os.getenv("GEMINI_API_KEY", "")
    if not api_key:
        raise HTTPException(503, "OCR is not configured. Add GEMINI_API_KEY to backend/.env, or use manual entry.")
    if not files or len(files) > MAX_PAGES:
        raise HTTPException(400, f"Upload 1–{MAX_PAGES} files")
    pages = []
    for f in files:
        if not f.filename or not f.filename.lower().endswith((".pdf", ".jpg", ".jpeg", ".png", ".webp")):
            raise HTTPException(400, "Use PDF, JPG, PNG or WEBP files")
        data = await f.read(MAX_FILE + 1)
        if len(data) > MAX_FILE:
            raise HTTPException(413, "File is larger than 16 MB")
        for image in convert_to_images(data, f.filename):
            if len(pages) >= MAX_PAGES:
                raise HTTPException(400, f"Maximum {MAX_PAGES} pages per scan")
            recognized = await recognize_image(image, api_key)
            pages.append({"filename": f.filename, **recognized})
    return verify(pages)
