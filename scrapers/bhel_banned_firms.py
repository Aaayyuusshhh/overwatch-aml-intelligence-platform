"""
BHEL Banned Firms scraper (#139).

Source: https://www.bhel.com/list-debarred-firms (HTML page links to PDF)
PDF:    https://www.bhel.com/sites/default/files/Banned_firms_list_BHEL_<DATE>.pdf

The PDF has multiple 5-column tables: S.No | Supplier Name | Address |
Period/Reference | Start Date. The first table on page 1 has a 1-row
title "Firms Debarred by BHEL" that the generic engine misclassified as
a header. Custom scraper picks tables by their canonical column 1 =
'Supplier Name'.
"""

import csv
import os
import re
from datetime import datetime

import pdfplumber
from scrapling import Fetcher

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LIST_URL = "https://www.bhel.com/list-debarred-firms"
OUTPUT_FILE = os.path.join(PROJECT_ROOT, "data", "bhel_banned_firms_139.csv")
RAW_DIR = os.path.join(PROJECT_ROOT, "data", "raw")

CSV_FIELDS = [
    "source_agency", "source_list", "case_unit",
    "name", "father_name", "date_of_birth", "gender",
    "address", "reward_amount", "details",
    "has_document", "document_url", "detail_page_url",
    "interpol_notice_id", "link_kind", "scraped_at",
    "enrichment_status",
]


def _clean(v):
    if v is None:
        return ""
    s = str(v).replace("\n", " ")
    return re.sub(r"\s+", " ", s).strip()


def _discover_pdf_url():
    r = Fetcher.get(LIST_URL, timeout=45, retries=1, retry_delay=0, verify=False)
    body = r.body if hasattr(r, "body") else r.content
    if isinstance(body, bytes):
        body = body.decode("utf-8", "ignore")
    # BHEL renamed the PDF (Apr 2026): was Banned_firms_list_BHEL_<date>.pdf,
    # now "Debarred Firms List - BHEL.pdf". Match either filename pattern by
    # falling back to any anchor whose href looks like a debarred/banned PDF.
    patterns = [
        r'''href=["']([^"']*Banned_firms_list_BHEL[^"']*\.pdf[^"']*)["']''',
        r'''href=["']([^"']*Debarred[^"']*Firms[^"']*\.pdf[^"']*)["']''',
        r'''href=["']([^"']*Banned[^"']*Firms[^"']*\.pdf[^"']*)["']''',
    ]
    m = None
    for p in patterns:
        m = re.search(p, body, re.I)
        if m:
            break
    if not m:
        raise RuntimeError("BHEL: no debarred/banned-firms PDF anchor on /list-debarred-firms")
    href = m.group(1)
    if href.startswith("http"):
        return href
    return "https://www.bhel.com" + href


def _download(url):
    os.makedirs(RAW_DIR, exist_ok=True)
    dest = os.path.join(RAW_DIR, "bhel_banned_firms_139.pdf")
    r = Fetcher.get(url, timeout=60, retries=1, retry_delay=0, verify=False)
    body = r.body
    if isinstance(body, str):
        body = body.encode("utf-8", "replace")
    with open(dest, "wb") as f:
        f.write(body)
    return dest


def _parse_pdf(pdf_path, doc_url, scraped_at):
    rows_out = []
    with pdfplumber.open(pdf_path) as pdf:
        for pno, page in enumerate(pdf.pages, start=1):
            for t in page.extract_tables() or []:
                if not t or len(t) < 2:
                    continue
                # Locate header row: must have 'Supplier Name' in col 1.
                header_idx = None
                for i, r in enumerate(t):
                    if len(r) >= 2 and "supplier" in (str(r[1] or "")).lower():
                        header_idx = i
                        break
                if header_idx is None:
                    continue
                header = [_clean(c) for c in t[header_idx]]
                period_or_ref_label = header[3] if len(header) > 3 else "Period/Reference"
                for r in t[header_idx + 1:]:
                    if not r or all(not _clean(c) for c in r):
                        continue
                    sno      = _clean(r[0]) if len(r) > 0 else ""
                    supplier = _clean(r[1]) if len(r) > 1 else ""
                    addr     = _clean(r[2]) if len(r) > 2 else ""
                    period   = _clean(r[3]) if len(r) > 3 else ""
                    start    = _clean(r[4]) if len(r) > 4 else ""
                    if not supplier:
                        continue
                    details_parts = []
                    if period:
                        details_parts.append(f"{period_or_ref_label}: {period}")
                    if start:
                        details_parts.append(f"Start Date: {start}")
                    if sno:
                        details_parts.append(f"S.No: {sno}")
                    rows_out.append({
                        "source_agency": "BHEL",
                        "source_list": "Banned/Debarred Firms",
                        "case_unit": sno,
                        "name": supplier,
                        "father_name": "",
                        "date_of_birth": "",
                        "gender": "",
                        "address": addr,
                        "reward_amount": "",
                        "details": " | ".join(details_parts),
                        "has_document": "Yes",
                        "document_url": doc_url,
                        "detail_page_url": LIST_URL,
                        "interpol_notice_id": "",
                        "link_kind": "bhel_debarred",
                        "scraped_at": scraped_at,
                        "enrichment_status": "none",
                    })
    return rows_out


def save_to_csv(rows, out_path):
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    with open(out_path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=CSV_FIELDS)
        w.writeheader()
        for r in rows:
            w.writerow({k: r.get(k, "") for k in CSV_FIELDS})
    print(f"Saved {len(rows)} records to {out_path}")


def run():
    print("=" * 60)
    print("BHEL Banned Firms scraper (#139)")
    print("=" * 60)
    pdf_url = _discover_pdf_url()
    print(f"  pdf url: {pdf_url}")
    pdf_path = _download(pdf_url)
    scraped_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    rows = _parse_pdf(pdf_path, pdf_url, scraped_at)
    if not rows:
        raise RuntimeError("BHEL: zero rows parsed")
    save_to_csv(rows, OUTPUT_FILE)
    return rows


if __name__ == "__main__":
    run()
