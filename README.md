# Sumly — Price Schedule Scanner

A responsive, pastel-colored web app for multi-page price schedule scanning and calculation verification. It handles handwritten and printed amounts using a **configurable Google Gemini vision OCR** backend, then independently computes row totals and page/overall totals using Python's Decimal arithmetic. A manual-entry mode works with no OCR key.

## Core behavior
- **No handwritten total?** Displays a new calculated total (Calculated).
- **Handwritten total present?** Compares with independently calculated total (Correct / Incorrect).
- **Mixed document?** Checks present amounts and calculates missing ones, row by row.
- Prices written as `amount per N` are normalized to a per-unit price; quantities written as `required N` are recognized even without a quantity column.
- When a row contains prices from multiple suppliers, the lowest VAT-exclusive unit price is selected and the VAT-exclusive total is calculated separately as quantity × selected unit price. If VAT-exclusive quotes are unavailable, the lowest comparable VAT-inclusive unit price is selected.
- **Uncertain/missing quantity or both VAT-inclusive and VAT-exclusive prices?** Flags the row as Needs review; does not invent totals.
- When VAT-inclusive amounts are absent, uses the scanned VAT-exclusive unit prices and totals and reports a VAT-exclusive grand total without inventing a VAT rate.
- Uses the scanned grand total's VAT basis when one is provided; when no grand total is given and every row has a calculated VAT-inclusive total, displays the VAT-inclusive total.
- Verifies bottom-of-page grand totals separately and combines pages when all rows are complete.
- Editable OCR fields, optional handwritten grand total, add/delete rows and pages, CSV export.
- After scanning, an optional VAT rate prompt can calculate VAT-inclusive bulk prices, unit prices and totals from VAT-exclusive prices, including VAT-inclusive row totals based on a supplied or calculated VAT-exclusive total. Enter `18` or `18%` for an 18% rate; `1.18` or `1.18%` means 1.18%. Skip it to retain scanned inclusive values and show dashes with a Not given badge where inclusive values were not scanned.

**Important:** AI-based handwriting recognition is imperfect. A human must review ambiguous numerals. This is a verification aid, not certified accounting software. The application cannot guarantee every handwritten digit is extracted accurately.

## Local setup (Windows PowerShell)

### Backend (Python 3.11–3.13)
```powershell
cd backend
py -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
Copy-Item .env.example .env
notepad .env
python -m uvicorn main:app --reload --port 8000
```
To enable automatic handwriting OCR: set `GEMINI_API_KEY=your_key_here` inside `backend/.env`. Obtain a key from [Google AI Studio](https://aistudio.google.com/apikey). Keep it secret. OCR requests transmit uploaded page images to Google's API under its applicable terms. Verify provider terms/data-handling requirements before uploading confidential price schedules. API calls can incur charges, rate limits or regional restrictions. Leave the key blank for manual entry only.

If pip fails due to your organization's SSL certificate chain, **prefer** installing your organization's root certificate rather than bypassing TLS. On a managed Windows computer, consult IT. As a temporary workaround in a trusted environment only, try:
```powershell
python -m pip install --trusted-host pypi.org --trusted-host files.pythonhosted.org --timeout 200 -r requirements.txt
```

### Frontend (Node.js 20+)
Open **another** terminal:
```powershell
cd frontend
npm install
npm run dev
```
Browse **http://localhost:5173**. The Vite dev server proxies `/api` requests to FastAPI on port 8000. Use **Upload documents** for PDFs/photos; use **Review & calculate → Start manually** without an OCR key.

## Deployment
**Backend:** Deploy `backend/` as a Python web service on Render, Railway, or a Docker host. Install `pip install -r requirements.txt`; run `uvicorn main:app --host 0.0.0.0 --port $PORT` (platform-specific syntax). Add secret `GEMINI_API_KEY` and `CORS_ORIGINS=https://YOUR-FRONTEND-DOMAIN` as backend environment variables. A container option is provided via `backend/Dockerfile`.

**Frontend:** Deploy `frontend/` to Vercel or Netlify; build command `npm run build`, output directory `dist`; environment variable `VITE_API_URL=https://YOUR-BACKEND-DOMAIN` (do not add `/api`). Redeploy after setting the variable. Deploy frontend/backend over HTTPS.

**Security:** No database or permanent file storage is used. Documents are held in memory during processing. Use private infrastructure / provider agreements for sensitive price schedules; production should add authentication, per-user rate limits, tighter document constraints, malware scanning and privacy controls. Do not expose the OCR API key in the frontend. Default CORS is for local development only.

## Local sample documents
Sample documents may be kept locally for manual testing, but the `samples/` directory is excluded from Git because uploaded documents may contain confidential information. The application does not require this directory at runtime.

## API
- `GET /api/health` — OCR setup status
- `POST /api/scan` — multipart `files` (PDF or images); calls provider for OCR; returns independently verified amounts
- `POST /api/calculate` — JSON `{"pages":[{"filename":"Page 1","rows":[{"item":"Item A","quantity":"50","unit_price_with_vat":"16200","document_total":"810000"}],"document_grand_total":"810000"}]}`; recalculates without needing OCR. `vat_multiplier` and the table-indexed `vat_multipliers` accept percentage rates with or without `%`, such as `18`, `18%`, `1.18`, or `1.18%`; the rate is applied as `exclusive amount × (1 + rate / 100)`.

**Limitations:** Tables with merged cells, faint Sinhala handwriting, skewed photos or unusual pricing formats require review. PDF documents support up to 12 pages per request, max 16 MB per uploaded file. VAT is never inferred; it is only applied when the user supplies a rate. The status "Calculated" means the corresponding document value was blank or derived from the supplied VAT rate; pre-existing values are marked Correct or Incorrect.
