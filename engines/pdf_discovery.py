"""
engines/pdf_discovery.py

Discover-and-parse fallback for source pages whose primary HTML table
is empty or thin, but whose page links to one or more downloadable
files (PDF, XLSX, XLS, CSV, DOC, DOCX) containing the actual list.
This pattern is widespread on Indian regulator sites: the landing
page has a brief description and a "Download List" button.

Pipeline
--------
fetch_page(url)              ↘
                              find_attachment_links(html)
                                                      ↘
                                                       per file:
                                                         dispatch_parser(file_url)
                                                                     ↘
                                                                      records
                                                                      ↘
                                                          dedupe(records)
                                                                      ↘
                                                                      CSV

Parser dispatch
---------------
.pdf            -> engines/pdf_scraper.py::run() (table -> text -> OCR)
.xlsx / .xls    -> pandas.read_excel
.csv            -> pandas.read_csv
.doc / .docx    -> not implemented; logged + skipped (no python-docx
                   in requirements.txt). We surface the file path so an
                   engineer can hand-process if it's high-value.

AML filter
----------
Only download files whose URL or anchor text contains an AML-relevant
keyword (defaulter, blacklist, banned, etc.). This avoids wasting
bandwidth on annual reports / FAQs / policy circulars.

Public API
----------
run(source) -> result dict (same shape as engines/{html,pdf}_scraper)
    - status:  "success" | "skipped" | "failure"
    - record_count, csv_path, error, fetch_tier="discovery", ...

discover(url) -> list[dict]
    Returns metadata for every AML-relevant attachment on the page,
    without downloading. Useful for diagnostic scripts.
"""

import csv
import io
import os
import re
import time
from datetime import datetime
from urllib.parse import urljoin

from scrapling import Fetcher

from engines import pdf_scraper

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.path.join(PROJECT_ROOT, "data")
RAW_DIR = os.path.join(PROJECT_ROOT, "data", "raw")

CSV_FIELDS = pdf_scraper.CSV_FIELDS

KEYWORDS = (
    "defaulter", "blacklist", "blacklisted", "debarred", "banned", "banning",
    "suspended", "suspension", "wanted", "penalty", "penalties", "wilful",
    "wilfull", "expelled", "watchlist", "offender", "fraud", "convicted",
    "disqualified", "absconder", "cancelled", "fugitive", "proclaimed",
)
SUPPORTED_EXTS = (".pdf", ".xlsx", ".xls", ".csv", ".doc", ".docx")
ANCHOR_RE = re.compile(
    r'''<a[^>]+href=["']([^"']+)["'][^>]*>([\s\S]{0,400}?)</a>''', re.I)
TAG_RE = re.compile(r"<[^>]+>")


# ---------------------------------------------------------------------------
# Discovery
# ---------------------------------------------------------------------------
def _anchor_text(raw):
    return re.sub(r"\s+", " ", TAG_RE.sub(" ", raw)).strip()


def _is_attachment(href):
    h = href.lower().split("?")[0]
    return h.endswith(SUPPORTED_EXTS)


def _ext(url):
    return os.path.splitext(url.split("?")[0])[1].lower().lstrip(".")


def discover(url):
    """Fetch the landing page (static) and return AML-relevant
    attachment metadata. Each dict: {file_url, link_text, file_type,
    matched_kw}."""
    try:
        r = Fetcher.get(url, timeout=30, retries=1, retry_delay=0,
                        verify=False)
    except Exception as e:
        return [{"error": f"{type(e).__name__}: {str(e)[:140]}"}]
    body = r.body if hasattr(r, "body") else r.content
    if isinstance(body, bytes):
        body = body.decode("utf-8", "ignore")
    if getattr(r, "status", None) is None or r.status >= 400:
        return [{"error": f"http_status={getattr(r, 'status', None)}"}]

    out = []
    seen = set()
    for m in ANCHOR_RE.finditer(body):
        href, raw = m.group(1), m.group(2)
        if not _is_attachment(href):
            continue
        txt = _anchor_text(raw)
        blob = (href + " " + txt).lower()
        kws = [k for k in KEYWORDS if k in blob]
        if not kws:
            continue
        full = urljoin(url, href.strip())
        if full in seen:
            continue
        seen.add(full)
        out.append({
            "file_url": full,
            "link_text": txt[:200],
            "file_type": _ext(full),
            "matched_kw": ",".join(kws[:3]),
        })
    return out


# ---------------------------------------------------------------------------
# Parsers
# ---------------------------------------------------------------------------
def _row_to_record(source, row_dict, file_url, scraped_at, link_kind,
                   confidence="medium"):
    """Map a parsed row dict into the 17-column schema. The first
    string-y value becomes the name; the rest land in details."""
    name = ""
    for k, v in row_dict.items():
        if v and isinstance(v, str) and len(v.strip()) >= 4:
            name = v.strip()
            break
    if not name:
        return None
    details = " | ".join(f"{k}: {v}" for k, v in row_dict.items()
                         if v and str(v).strip())
    return {
        "source_agency": source["agency"],
        "source_list": source["list_name"],
        "case_unit": "",
        "name": name,
        "father_name": "",
        "date_of_birth": "",
        "gender": "",
        "address": "",
        "reward_amount": "",
        "details": details[:1500],
        "has_document": "Yes",
        "document_url": file_url,
        "detail_page_url": source.get("url", ""),
        "interpol_notice_id": "",
        "link_kind": link_kind,
        "scraped_at": scraped_at,
        "enrichment_status": "none",
    }


def _parse_pdf(file_url, source, scraped_at):
    """Reuse engines.pdf_scraper.run by synthesising a sub-source."""
    sub = {
        "id": f"{source['id']}__attach_{abs(hash(file_url)) % (10**6)}",
        "agency": source["agency"],
        "list_name": source["list_name"],
        "url": file_url,
        "type": "pdf",
    }
    res = pdf_scraper.run(sub)
    out = []
    if res["status"] == "success" and res["csv_path"]:
        with open(res["csv_path"], "r", encoding="utf-8", newline="") as f:
            for row in csv.DictReader(f):
                # Re-stamp source identity (sub-source had a synthetic id).
                row["source_agency"] = source["agency"]
                row["source_list"] = source["list_name"]
                row["detail_page_url"] = source.get("url", "")
                out.append(row)
        # Clean up the per-attachment CSV; we'll merge under the parent.
        try:
            os.remove(res["csv_path"])
        except Exception:
            pass
    return out


def _parse_xlsx(file_url, source, scraped_at, link_kind="xlsx_attached"):
    import pandas as pd
    try:
        r = Fetcher.get(file_url, timeout=120, retries=1, retry_delay=0,
                        verify=False)
        body = r.body
        if isinstance(body, str):
            body = body.encode("utf-8", "replace")
        sheets = pd.read_excel(io.BytesIO(body), sheet_name=None,
                               header=None)
    except Exception as e:
        print(f"[pdf_discovery] xlsx parse failed for {file_url}: "
              f"{type(e).__name__}: {e}")
        return []
    out = []
    for sn, df in sheets.items():
        if df.shape[0] < 2:
            continue
        # Use first non-blank row as header.
        header = None
        for i in range(min(15, len(df))):
            row_vals = df.iloc[i].dropna().tolist()
            if len(row_vals) >= 2 and any(isinstance(v, str) for v in row_vals):
                header = [str(c).strip() for c in df.iloc[i].fillna("")]
                start = i + 1
                break
        if header is None:
            continue
        for _, row in df.iloc[start:].iterrows():
            cells = [str(c).strip() if not (c is None) else ""
                     for c in row.fillna("")]
            row_dict = {h: v for h, v in zip(header, cells) if h or v}
            rec = _row_to_record(source, row_dict, file_url, scraped_at,
                                 link_kind)
            if rec:
                out.append(rec)
    return out


def _parse_csv_file(file_url, source, scraped_at):
    import pandas as pd
    try:
        r = Fetcher.get(file_url, timeout=60, retries=1, retry_delay=0,
                        verify=False)
        body = r.body if hasattr(r, "body") else r.content
        if isinstance(body, bytes):
            body = body.decode("utf-8", "ignore")
        df = pd.read_csv(io.StringIO(body))
    except Exception as e:
        print(f"[pdf_discovery] csv parse failed: {type(e).__name__}: {e}")
        return []
    out = []
    for _, row in df.iterrows():
        row_dict = {k: ("" if v is None else str(v).strip())
                    for k, v in row.items()}
        rec = _row_to_record(source, row_dict, file_url, scraped_at,
                             "csv_attached")
        if rec:
            out.append(rec)
    return out


def _parse_dispatch(att, source, scraped_at):
    ftype = att["file_type"]
    if ftype == "pdf":
        return _parse_pdf(att["file_url"], source, scraped_at)
    if ftype in ("xlsx", "xls"):
        return _parse_xlsx(att["file_url"], source, scraped_at)
    if ftype == "csv":
        return _parse_csv_file(att["file_url"], source, scraped_at)
    if ftype in ("doc", "docx"):
        print(f"[pdf_discovery] {ftype.upper()} not supported: "
              f"{att['file_url']}")
        return []
    return []


# ---------------------------------------------------------------------------
# Dedup + CSV write
# ---------------------------------------------------------------------------
def _dedupe(records):
    seen, out = set(), []
    for r in records:
        key = (r.get("name", "").strip().lower(),
               r.get("source_agency", "").strip().lower())
        if not key[0]:
            continue
        if key in seen:
            continue
        seen.add(key)
        out.append(r)
    return out


def _save_csv(records, out_path):
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    with open(out_path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=CSV_FIELDS)
        w.writeheader()
        for r in records:
            w.writerow({k: r.get(k, "") for k in CSV_FIELDS})


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------
def run(source):
    """Discover + parse all AML-relevant attachments on source['url'].
    Returns the standard result dict; CSV is written to data/<id>.csv.
    Falls back to status=skipped (not failure) when no attachments are
    found, so the caller can keep its existing thin extraction without
    losing it to a 'failure' alert."""
    start = time.time()
    sid = source["id"]
    url = source.get("url")
    out_path = os.path.join(DATA_DIR, f"{sid}.csv")
    base = {
        "status": "failure", "record_count": 0,
        "runtime_seconds": 0.0, "error": None, "csv_path": None,
        "extraction_strategy": "discovery_failed",
        "fetch_tier": "discovery",
        "attachments_found": 0, "attachments_parsed": 0,
    }
    if not url:
        base["error"] = "no url in source config"
        base["runtime_seconds"] = round(time.time() - start, 2)
        return base

    atts = discover(url)
    atts = [a for a in atts if "error" not in a]
    base["attachments_found"] = len(atts)
    if not atts:
        base["status"] = "skipped"
        base["error"] = "no AML-relevant attachments discovered"
        base["runtime_seconds"] = round(time.time() - start, 2)
        return base

    scraped_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    all_records = []
    parsed_ok = 0
    for att in atts:
        try:
            recs = _parse_dispatch(att, source, scraped_at)
        except Exception as e:
            print(f"[pdf_discovery] parse failed for {att['file_url']}: "
                  f"{type(e).__name__}: {e}")
            recs = []
        if recs:
            parsed_ok += 1
            all_records.extend(recs)
    base["attachments_parsed"] = parsed_ok

    deduped = _dedupe(all_records)
    if not deduped:
        base["status"] = "skipped"
        base["error"] = "attachments produced 0 records"
        base["runtime_seconds"] = round(time.time() - start, 2)
        return base

    _save_csv(deduped, out_path)
    base["status"] = "success"
    base["record_count"] = len(deduped)
    base["csv_path"] = out_path
    base["extraction_strategy"] = "discovery"
    base["runtime_seconds"] = round(time.time() - start, 2)
    return base
