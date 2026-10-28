# Sumly — Procurement Calculation Scanner

A responsive, pastel-colored web app for multi-page procurement calculation verification. It handles handwritten and printed amounts using a **configurable Google Gemini vision OCR** backend, then independently computes row totals and page/overall totals using Python's Decimal arithmetic. A manual-entry mode works with no OCR key.

## Core behavior
- **No handwritten total?** Displays a new calculated total (Calculated).
- **Handwritten total present?** Compares with independently calculated total (Correct / Incorrect).
- **Mixed document?** Checks present amounts and calculates missing ones, row by row.
- **Uncertain/missing quantity or both VAT-inclusive and VAT-exclusive prices?** Flags the row as Needs review; does not invent totals.
- When VAT-inclusive amounts are absent, uses the scanned VAT-exclusive unit prices and totals and reports a VAT-exclusive grand total without inventing a VAT rate.
- Verifies bottom-of-page grand totals separately and combines pages when all rows are complete.
- Editable OCR fields, optional handwritten grand total, add/delete rows and pages, CSV export.

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
To enable automatic handwriting OCR: set `GEMINI_API_KEY=your_key_here` inside `backend/.env`. Obtain a key from [Google AI Studio](https://aistudio.google.com/apikey). Keep it secret. OCR requests transmit uploaded page images to Google's API under its applicable terms. Verify provider terms/data-handling requirements before uploading confidential procurement documents. API calls can incur charges, rate limits or regional restrictions. Leave the key blank for manual entry only.

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

**Security:** No database or permanent file storage is used. Documents are held in memory during processing. Use private infrastructure / provider agreements for sensitive procurements; production should add authentication, per-user rate limits, tighter document constraints, malware scanning and privacy controls. Do not expose the OCR API key in the frontend. Default CORS is for local development only.

## Local sample documents
Sample documents may be kept locally for manual testing, but the `samples/` directory is excluded from Git because uploaded documents may contain confidential information. The application does not require this directory at runtime.

## API
- `GET /api/health` — OCR setup status
- `POST /api/scan` — multipart `files` (PDF or images); calls provider for OCR; returns independently verified amounts
- `POST /api/calculate` — JSON `{"pages":[{"filename":"Page 1","rows":[{"item":"Item A","quantity":"50","unit_price_with_vat":"16200","document_total":"810000"}],"document_grand_total":"810000"}]}`; recalculates without needing OCR.

**Limitations:** Tables with merged cells, faint Sinhala handwriting, skewed photos or unusual pricing formats require review. PDF documents support up to 12 pages per request, max 16 MB per uploaded file. VAT is **not** computed or inferred; when VAT-inclusive values are absent, the scanner sums the VAT-exclusive values explicitly shown on the document. The status "Calculated" means the corresponding document total was blank, not that any pre-existing total was verified.
