"""
engines/pdf_scraper.py - Generic PDF extraction engine.

Per ARCHITECTURE.md S4.3 / PRD S6.3. Same cascading philosophy as
engines/html_scraper.py:

  Strategy 1 - tables : pdfplumber.extract_tables() across all pages,
                        merge multi-page tables that share headers,
                        pick the largest group with >= 3 data rows
  Strategy 2 - text   : extract_text() across all pages, save as
                        single 'unstructured' row

Output writes the 17-column shared schema. New link_kind values:
  - pdf_structured (table extraction)
  - unstructured   (raw text fallback - same value used by HTML engine)

Public entry point:
    run(source_config) -> result_dict
"""

import csv
import os
import re
import time
from datetime import datetime

import pdfplumber
from scrapling import Fetcher

# OCR (Strategy 3) - guarded imports. If tesseract / pytesseract /
# pdf2image isn't installed the engine still works for text+table PDFs.
try:
    import pytesseract
    from pdf2image import convert_from_path
    _OCR_AVAILABLE = True
    _OCR_ERR = None
except Exception as _e:                     # pragma: no cover
    _OCR_AVAILABLE = False
    _OCR_ERR = f"{type(_e).__name__}: {_e}"

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.path.join(PROJECT_ROOT, "data")
RAW_DIR = os.path.join(PROJECT_ROOT, "data", "raw")

CSV_FIELDS = [
    "source_agency", "source_list", "case_unit",
    "name", "father_name", "date_of_birth", "gender",
    "address", "reward_amount", "details",
    "has_document", "document_url", "detail_page_url",
    "interpol_notice_id", "link_kind", "scraped_at",
    "enrichment_status",
]

DOWNLOAD_TIMEOUT = 60       # seconds
MIN_TABLE_ROWS = 3          # minimum extracted data rows for table strategy
EMPTY_PDF_MIN_CHARS = 50    # below this -> empty_pdf
RAW_TEXT_LIMIT = 5000
NAV_THRESHOLD = 0.30
POLITENESS_SECONDS = 2.0
OCR_DPI = 200               # rasterisation DPI for OCR (200 is a good speed/accuracy trade-off)
OCR_MAX_PAGES = 50          # safety cap; OCR is slow (~3-10s/page)

NAV_TERMS = {
    "home", "about", "contact", "login", "search", "menu",
    "back", "next", "previous", "submit", "click here",
    "read more", "download", "skip to content",
}

BOT_BLOCK_PHRASES = (
    "access denied",
    "please enable javascript",
    "javascript is required",
    "captcha required",
    "verify you are human",
    "the requested url was rejected",
)

# Header-text -> schema-column matchers.
NAME_HEADERS = ("name", "borrower", "party", "firm", "company",
                "individual", "consultant", "contractor", "vendor",
                "agency", "entity", "organisation", "wanted",
                "accused", "defaulter", "person", "guarantor")
ADDRESS_HEADERS = ("address", "registered office", "location",
                   "city", "state", "district", "residence")
AMOUNT_HEADERS = ("amount", "outstanding", "balance", "dues",
                  "principal", "loan", "reward", "penalty",
                  "crystallised", "decree value", "decreed")
CASE_HEADERS = ("case", "fir", "ref", "reference no", "order no",
                "rc no", "s.no", "sno", "sr no", "sr.")
DOC_HEADERS = ("document", "pdf", "attachment", "order document")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _now():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _clean(s):
    if s is None:
        return ""
    s = str(s).replace("\xa0", " ")
    s = re.sub(r"\s+", " ", s).strip()
    return s


def _output_path(sid):
    return os.path.join(DATA_DIR, f"{sid}.csv")


def _empty_row(agency, source_list, scraped_at):
    return {
        "source_agency": agency,
        "source_list": source_list,
        "case_unit": "",
        "name": "",
        "father_name": "",
        "date_of_birth": "",
        "gender": "",
        "address": "",
        "reward_amount": "",
        "details": "",
        "has_document": "No",
        "document_url": "",
        "detail_page_url": "",
        "interpol_notice_id": "",
        "link_kind": "",
        "scraped_at": scraped_at,
        "enrichment_status": "none",
    }


def _save_csv(rows, out_path):
    if not rows:
        return 0
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    with open(out_path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=CSV_FIELDS)
        w.writeheader()
        for r in rows:
            w.writerow({k: r.get(k, "") for k in CSV_FIELDS})
    return len(rows)


def _match_header(h_lc, candidates):
    return any(c in h_lc for c in candidates)


def _classify_headers(headers):
    """Map header indices to schema fields. Same logic as the HTML engine
    but tuned for PDF table headers (which often include 'borrower',
    'principal outstanding', etc.)."""
    mapping = {}
    name_idx = None
    for i, h in enumerate(headers):
        h_lc = (h or "").lower()
        if not h_lc:
            continue
        if name_idx is None and _match_header(h_lc, NAME_HEADERS):
            mapping[i] = "name"
            name_idx = i
        elif _match_header(h_lc, ADDRESS_HEADERS):
            mapping[i] = "address"
        elif _match_header(h_lc, AMOUNT_HEADERS):
            mapping[i] = "reward_amount"
        elif _match_header(h_lc, CASE_HEADERS):
            mapping[i] = "case_unit"
        elif _match_header(h_lc, DOC_HEADERS):
            mapping[i] = "document_url"
    return mapping, name_idx


# ---------------------------------------------------------------------------
# PDF download (Scrapling, per project rule: no requests/urllib for HTTP)
# ---------------------------------------------------------------------------
_last_run_at = 0.0


def _polite_sleep():
    global _last_run_at
    elapsed = time.time() - _last_run_at
    if elapsed < POLITENESS_SECONDS:
        time.sleep(POLITENESS_SECONDS - elapsed)


def download_pdf(url, dest_path):
    """Returns (success_bool, http_status, err_msg). Saves PDF bytes to
    dest_path on success. Detects and surfaces bot-block / non-PDF
    bodies (HTML error pages with Cloudflare etc.)."""
    _polite_sleep()
    try:
        resp = Fetcher.get(url, timeout=DOWNLOAD_TIMEOUT, retries=1,
                           retry_delay=0, verify=False)
    except Exception as e:
        return False, None, f"fetch_error {type(e).__name__}: {str(e)[:150]}"
    status = getattr(resp, "status", None) or getattr(resp, "status_code", None)
    if status is None or status >= 400:
        return False, status, f"http_status={status}"

    body = getattr(resp, "body", None) or getattr(resp, "content", None)
    if isinstance(body, str):
        body = body.encode("utf-8", "replace")
    if not body or len(body) < 200:
        return False, status, "empty/too-small download body"

    # Sanity: must look like a PDF. Some bank sites serve a Cloudflare
    # JS-challenge HTML page when they detect a bot - flag those.
    if not body[:8].lstrip().startswith(b"%PDF"):
        snippet = body[:1500].decode("utf-8", "ignore").lower()
        for phrase in BOT_BLOCK_PHRASES:
            if phrase in snippet:
                return False, status, f"bot_block: {phrase!r}"
        return False, status, "downloaded body is not a PDF (got HTML?)"

    os.makedirs(os.path.dirname(dest_path), exist_ok=True)
    with open(dest_path, "wb") as f:
        f.write(body)
    return True, status, None


# ---------------------------------------------------------------------------
# Strategy 1: tables across pages, merged where headers match
# ---------------------------------------------------------------------------
def _collect_tables(pdf):
    """Return list of (page_number, table_rows) for every table on every
    page that has at least 2 rows."""
    out = []
    for pno, page in enumerate(pdf.pages, start=1):
        try:
            tables = page.extract_tables() or []
        except Exception:
            tables = []
        for t in tables:
            if t and len(t) >= 2:
                out.append((pno, t))
    return out


def _merge_multipage_tables(raw_tables):
    """Group consecutive tables that share the same header row. Returns
    list of (headers, rows). Tables without consistent headers become
    their own group."""
    groups = []
    current = None
    for _pno, t in raw_tables:
        headers = [_clean(c) for c in t[0]]
        if not any(headers):
            continue
        rows = [[_clean(c) for c in r] for r in t[1:]]
        if not rows:
            continue
        if current is not None and current[0] == headers:
            current[1].extend(rows)
        else:
            current = (headers, list(rows))
            groups.append(current)
    return groups


def strategy_pdf_tables(pdf, source, scraped_at):
    """Return (rows_or_None, n_raw_tables_seen)."""
    raw = _collect_tables(pdf)
    if not raw:
        return None, 0
    groups = _merge_multipage_tables(raw)
    groups = [g for g in groups if len(g[1]) >= MIN_TABLE_ROWS]
    if not groups:
        return None, len(raw)
    groups.sort(key=lambda g: -len(g[1]))
    headers, rows = groups[0]
    header_map, name_idx = _classify_headers(headers)

    out = []
    for cells in rows:
        if not any((c or "").strip() for c in cells):
            continue
        if name_idx is None:
            first_idx = next((i for i, c in enumerate(cells) if (c or "").strip()), None)
            if first_idx is None:
                continue
            local_name_idx = first_idx
        else:
            local_name_idx = name_idx

        rec = _empty_row(source["agency"], source["list_name"], scraped_at)
        rec["link_kind"] = "pdf_structured"
        rec["name"] = cells[local_name_idx] if local_name_idx < len(cells) else ""

        details_pairs = []
        for i, val in enumerate(cells):
            if i == local_name_idx:
                continue
            field = header_map.get(i)
            header = headers[i] if i < len(headers) else f"col_{i}"
            if field and field != "name":
                if rec.get(field):
                    rec[field] = rec[field] + " | " + val
                else:
                    rec[field] = val
            else:
                if val:
                    details_pairs.append(f"{header}: {val}")
        if details_pairs:
            rec["details"] = " | ".join(details_pairs)

        if rec["name"]:
            out.append(rec)
    if len(out) < MIN_TABLE_ROWS:
        return None, len(raw)
    return out, len(raw)


# ---------------------------------------------------------------------------
# Strategy 2: text fallback
# ---------------------------------------------------------------------------
def strategy_pdf_text(pdf, source, scraped_at, source_url):
    parts = []
    for page in pdf.pages:
        try:
            t = page.extract_text() or ""
        except Exception:
            t = ""
        if t:
            parts.append(t)
    text = _clean("\n".join(parts))
    if len(text) < EMPTY_PDF_MIN_CHARS:
        return None
    rec = _empty_row(source["agency"], source["list_name"], scraped_at)
    rec["link_kind"] = "unstructured"
    rec["details"] = text[:RAW_TEXT_LIMIT]
    rec["detail_page_url"] = source_url
    return [rec]


# ---------------------------------------------------------------------------
# Strategy 3: OCR (for scanned / image-only PDFs)
# ---------------------------------------------------------------------------
def _ocr_pdf(pdf_path, max_pages=OCR_MAX_PAGES):
    """Rasterise the PDF and run Tesseract on each page. Returns the
    concatenated OCR text, or '' if OCR is unavailable / produces nothing."""
    if not _OCR_AVAILABLE:
        return ""
    try:
        # poppler-utils provides pdftoppm which pdf2image uses.
        images = convert_from_path(pdf_path, dpi=OCR_DPI, last_page=max_pages)
    except Exception as e:
        print(f"  pdf2image error: {type(e).__name__}: {str(e)[:120]}")
        return ""
    pieces = []
    for i, img in enumerate(images):
        try:
            t = pytesseract.image_to_string(img)
        except Exception as e:
            # Most likely cause: tesseract binary not on PATH.
            print(f"  pytesseract error on page {i+1}: {type(e).__name__}: {str(e)[:120]}")
            return ""
        if t:
            pieces.append(t)
    return "\n".join(pieces)


def _parse_ocr_text_into_rows(ocr_text, source, scraped_at, source_url):
    """Turn OCR text into structured rows.

    Heuristic: split on lines, skip header-like / very-short lines,
    treat each remaining line as a candidate record. Lines beginning
    with a sequence number ('1.', '2)', etc.) are stripped to leave
    just the name / details.

    The result tags link_kind = 'pdf_ocr' and puts the recognised
    line in 'name' with the full line in 'details'."""
    if not ocr_text or len(ocr_text) < EMPTY_PDF_MIN_CHARS:
        return None
    lines = []
    for line in ocr_text.splitlines():
        s = _clean(line)
        if not s or len(s) < 3:
            continue
        # Skip obvious page-chrome lines.
        low = s.lower()
        if low.startswith(("page ", "sl no", "sr no", "s.no", "s. no", "name of",
                           "address", "date", "f/n", "father")):
            continue
        # Strip leading numbering: '1.', '12)', '15 -', etc.
        s2 = re.sub(r"^\d+\s*[\.\)\-:]\s*", "", s).strip()
        if len(s2) < 3:
            continue
        lines.append(s2)
    if len(lines) < MIN_TABLE_ROWS:
        return None

    out = []
    for line in lines:
        rec = _empty_row(source["agency"], source["list_name"], scraped_at)
        rec["link_kind"] = "pdf_ocr"
        # Take first ~100 chars as name candidate; rest into details.
        first_segment = line[:120]
        rec["name"] = first_segment
        rec["details"] = line[:RAW_TEXT_LIMIT]
        rec["detail_page_url"] = source_url
        out.append(rec)
    return out


def strategy_pdf_ocr(pdf_path, source, scraped_at, source_url):
    if not _OCR_AVAILABLE:
        return None
    print(f"  OCR: rasterising + tesseract on {pdf_path}")
    text = _ocr_pdf(pdf_path)
    if not text:
        return None
    rows = _parse_ocr_text_into_rows(text, source, scraped_at, source_url)
    return rows


# ---------------------------------------------------------------------------
# Quality guards
# ---------------------------------------------------------------------------
def _detect_nav_garbage(rows):
    if not rows:
        return False
    nav = sum(1 for r in rows if (r.get("name") or "").strip().lower() in NAV_TERMS)
    return (nav / len(rows)) > NAV_THRESHOLD


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------
def run(source):
    """Run the generic PDF engine for one source. Returns the framework
    result dict augmented with PDF-specific metrics."""
    global _last_run_at
    start = time.time()
    sid = source["id"]
    url = source.get("url")
    out_path = _output_path(sid)

    base = {
        "status": "failure", "record_count": 0,
        "runtime_seconds": 0.0,
        "error": None, "csv_path": None,
        "extraction_strategy": "pdf_failed",
        "fetch_time_seconds": 0.0,
        "parse_time_seconds": 0.0,
        "rows_extracted": 0,
        "http_status_code": None,
        "pages_processed": 0,
        "tables_found": 0,
        "fallback_used": False,
    }

    if not url:
        base["error"] = "no url in source config"
        base["runtime_seconds"] = round(time.time() - start, 2)
        _last_run_at = time.time()
        return base

    # Download
    fetch_t0 = time.time()
    pdf_path = os.path.join(RAW_DIR, f"{sid}.pdf")
    ok, http_status, err = download_pdf(url, pdf_path)
    base["fetch_time_seconds"] = round(time.time() - fetch_t0, 2)
    base["http_status_code"] = http_status
    _last_run_at = time.time()

    if not ok:
        base["error"] = err
        # bot-block downgrades to skipped (not failure) so the orchestrator
        # doesn't burn an alert on each re-run; engineer reclassifies in
        # sources.json.
        if err and "bot_block" in err:
            base["status"] = "skipped"
        base["runtime_seconds"] = round(time.time() - start, 2)
        return base

    # Parse
    parse_t0 = time.time()
    scraped_at = _now()
    rows = None
    strategy = None
    try:
        with pdfplumber.open(pdf_path) as pdf:
            base["pages_processed"] = len(pdf.pages)
            try:
                rows, n_tables = strategy_pdf_tables(pdf, source, scraped_at)
                base["tables_found"] = n_tables
                if rows:
                    if _detect_nav_garbage(rows):
                        rows = None
                    else:
                        strategy = "pdf_table"
            except Exception as e:
                print(f"  strategy_pdf_tables raised {type(e).__name__}: {e}")
                rows = None

            if not rows:
                base["fallback_used"] = True
                rows = strategy_pdf_text(pdf, source, scraped_at, url)
                if rows:
                    strategy = "pdf_text"
                    # pdf_text always collapses everything into a single
                    # 'unstructured' row. If that's all we got, the PDF
                    # has no real machine-readable structure - escalate
                    # to OCR (assuming tesseract is available).
                    if len(rows) == 1 and _OCR_AVAILABLE:
                        rows = None
                        strategy = None
            if not rows and _OCR_AVAILABLE:
                ocr_rows = strategy_pdf_ocr(pdf_path, source, scraped_at, url)
                if ocr_rows:
                    rows = ocr_rows
                    strategy = "pdf_ocr"
    except Exception as e:
        base["error"] = f"parse_error {type(e).__name__}: {str(e)[:150]}"
        base["parse_time_seconds"] = round(time.time() - parse_t0, 2)
        base["runtime_seconds"] = round(time.time() - start, 2)
        return base
    base["parse_time_seconds"] = round(time.time() - parse_t0, 2)

    if not rows:
        base["error"] = "empty_pdf or no extractable content"
        base["runtime_seconds"] = round(time.time() - start, 2)
        return base

    n = _save_csv(rows, out_path)
    base["status"] = "success"
    base["record_count"] = n
    base["rows_extracted"] = n
    base["csv_path"] = out_path
    base["extraction_strategy"] = strategy
    base["runtime_seconds"] = round(time.time() - start, 2)
    return base
