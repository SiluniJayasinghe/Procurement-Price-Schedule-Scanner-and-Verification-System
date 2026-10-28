"""Procurement Calculation Scanner: multimodal OCR assisted with exact Decimal arithmetic."""
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
app = FastAPI(title="Procurement Calculation Scanner", version="1.0.0")
origins = os.getenv("CORS_ORIGINS", "http://localhost:5173,http://127.0.0.1:5173").split(",")
app.add_middleware(CORSMiddleware, allow_origins=[s.strip() for s in origins], allow_credentials=False, allow_methods=["*"], allow_headers=["*"])
MAX_FILE = 16 * 1024 * 1024
MAX_PAGES = 12
MODEL = os.getenv("GEMINI_MODEL", "gemini-2.5-flash")
MONEY = Decimal("0.01")

class CheckRequest(BaseModel):
    pages: list[dict[str, Any]]

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

def fmt(value):
    return str(value.quantize(MONEY, rounding=ROUND_HALF_UP)) if value is not None else None

def verify(pages):
    output = []
    overall = Decimal("0")
    all_page_complete = True
    recorded_grand_totals = [
        (decimal_or_none(page.get("document_grand_total")), decimal_or_none(page.get("document_grand_total_without_vat")))
        for page in pages
    ]
    for pi, page in enumerate(pages):
        rows_out = []
        page_total = Decimal("0")
        complete = True
        for ri, row in enumerate(page.get("rows", [])):
            quantity = decimal_or_none(row.get("quantity"))
            unit_price_with_vat = decimal_or_none(row.get("unit_price_with_vat"))
            unit_price_without_vat = decimal_or_none(row.get("unit_price_without_vat"))
            unit_price = unit_price_with_vat if unit_price_with_vat is not None else unit_price_without_vat
            provided_with_vat = decimal_or_none(row.get("document_total"))
            provided_without_vat = decimal_or_none(row.get("document_total_without_vat"))
            basis = "vat_inclusive" if unit_price_with_vat is not None else "vat_exclusive" if unit_price_without_vat is not None else None
            provided = provided_with_vat if basis == "vat_inclusive" else provided_without_vat
            if basis is None and provided_without_vat is not None:
                calculated = provided_without_vat.quantize(MONEY, rounding=ROUND_HALF_UP)
                basis = "vat_exclusive"
                page_total += calculated
                status = "written_total"
            elif basis is None and provided_with_vat is not None:
                calculated = provided_with_vat.quantize(MONEY, rounding=ROUND_HALF_UP)
                basis = "vat_inclusive"
                page_total += calculated
                status = "written_total"
            elif quantity is None or unit_price is None or quantity < 0 or unit_price < 0:
                calculated = None
                status = "needs_review"
                complete = False
            else:
                calculated = (quantity * unit_price).quantize(MONEY, rounding=ROUND_HALF_UP)
                page_total += calculated
                status = "calculated" if provided is None else "correct" if provided.quantize(MONEY) == calculated else "incorrect"
            rows_out.append({**row, "row_index": ri, "calculation_basis": basis, "calculated_total": fmt(calculated), "difference": fmt(calculated-provided) if calculated is not None and provided is not None else None, "status": status})
        if not rows_out:
            complete = False
        recorded_with_vat, recorded_without_vat = recorded_grand_totals[pi]
        recorded = recorded_with_vat if recorded_with_vat is not None else recorded_without_vat
        page_basis = "vat_inclusive" if recorded_with_vat is not None else "vat_exclusive" if recorded_without_vat is not None or any(row["calculation_basis"] == "vat_exclusive" for row in rows_out) else None
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

    groups = []
    group_start = 0
    for index, (recorded_with_vat, recorded_without_vat) in enumerate(recorded_grand_totals):
        if recorded_with_vat is not None or recorded_without_vat is not None:
            groups.append((group_start, index))
            group_start = index + 1
    if group_start < len(output):
        groups.append((group_start, len(output) - 1))

    group_results = []
    for group_index, (start, end) in enumerate(groups):
        group_pages = output[start:end + 1]
        group_complete = all(page["calculated_grand_total"] is not None for page in group_pages)
        group_total = sum((Decimal(page["calculated_grand_total"]) for page in group_pages if page["calculated_grand_total"] is not None), Decimal("0"))
        recorded_with_vat, recorded_without_vat = recorded_grand_totals[end]
        recorded = recorded_with_vat if recorded_with_vat is not None else recorded_without_vat
        group_status = "needs_review" if not group_complete else "calculated" if recorded is None else "correct" if recorded.quantize(MONEY) == group_total else "incorrect"
        group_rows = [row for page in group_pages for row in page["rows"]]
        group_result = {"table_group_index": group_index, "table_group_spans_pages": end > start, "table_group_overall_total": fmt(group_total) if group_complete else None, "table_group_document_grand_total": fmt(recorded), "table_group_grand_total_difference": fmt(group_total - recorded) if group_complete and recorded is not None else None, "table_group_grand_total_status": group_status, "table_group_calculation_basis": "vat_inclusive" if recorded_with_vat is not None else "vat_exclusive" if recorded_without_vat is not None else None, "table_group_row_counts": {"correct": sum(row["status"] == "correct" for row in group_rows), "incorrect": sum(row["status"] == "incorrect" for row in group_rows), "calculated": sum(row["status"] == "calculated" for row in group_rows), "written_total": sum(row["status"] == "written_total" for row in group_rows), "needs_review": sum(row["status"] == "needs_review" for row in group_rows)}}
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
    return {"pages": output, "overall_total": overall_total, "overall_document_grand_total": overall_group["table_group_document_grand_total"] if overall_group else None, "overall_grand_total_difference": overall_group["table_group_grand_total_difference"] if overall_group else None, "overall_grand_total_status": overall_group["table_group_grand_total_status"] if overall_group else None, "overall_calculation_basis": overall_group["table_group_calculation_basis"] if overall_group else None, "table_groups": group_results, "complete": all_page_complete and bool(output)}

@app.get("/api/health")
def health():
    return {"ok": True, "ocr_configured": bool(os.getenv("GEMINI_API_KEY")), "model": MODEL}

@app.post("/api/calculate")
def calculate(data: CheckRequest):
    if len(data.pages) > MAX_PAGES:
        raise HTTPException(400, "Too many pages")
    return verify(data.pages)

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

PROMPT = """You extract financial tables from scanned Sinhala/English procurement documents. Printed AND handwritten digits are important. Read carefully, including handwritten row totals and page grand totals. For each document row extract item/reference as 'item', numeric quantity as 'quantity', VAT-inclusive unit price as 'unit_price_with_vat', VAT-exclusive unit price as 'unit_price_without_vat', VAT-inclusive line total as 'document_total', and VAT-exclusive line total as 'document_total_without_vat'. Extract VAT-inclusive and VAT-exclusive bottom-of-page grand totals as 'document_grand_total' and 'document_grand_total_without_vat'. Preserve the value in the matching field when the document labels it excluding VAT, without converting or calculating it. NEVER calculate figures yourself or fill blank printed fields; a genuinely blank handwritten total is null. Preserve visible written amounts even if mathematically wrong. If uncertain, output null for that value and add a short note in 'warnings'. Extract every item row; avoid summary/header rows. Return only strict JSON object of format {"rows":[{"item":"...","quantity":"50","unit_price_with_vat":"16200.00","unit_price_without_vat":null,"document_total":"810000.00","document_total_without_vat":null,"warnings":[]}],"document_grand_total":"6625000.00","document_grand_total_without_vat":null,"warnings":[]}. Numeric strings contain no commas or currency symbols. A missing field must be null. No Markdown. OCR uncertainty should be flagged in warnings; do not invent unreadable digits."""

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
                clean_rows.append({"item": str(row.get("item") or ""), "quantity": row.get("quantity"), "unit_price_with_vat": row.get("unit_price_with_vat"), "unit_price_without_vat": row.get("unit_price_without_vat"), "document_total": row.get("document_total"), "document_total_without_vat": row.get("document_total_without_vat"), "warnings": row.get("warnings") if isinstance(row.get("warnings"), list) else []})
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
