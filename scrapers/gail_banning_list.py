"""
GAIL Banning Holiday List scraper (#144).

The vendor zone landing page (gailebank.gail.co.in) doesn't expose the
banning list as a structured table. The actual list lives in
gailonline.com/pdf/others/Banning_Holiday_List_as_on_<DATE>.pdf — a
proper 7-column PDF table:

  Sl.No | Name Of Company | Effective Date | Address & Phone | Email
        | Vendor Code | PAN NO | GSTN NO

Custom scraper because the generic engine treats Sl.No as the name
column (it's column 0 and the first header it sees). We hard-code
column 1 = company name.
"""

import csv
import os
import re
from datetime import datetime

import pdfplumber
from scrapling import Fetcher

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LIST_URL = "https://gailebank.gail.co.in/VendorZone/VendorZoneNew.html"
PDF_URL  = ("https://gailonline.com/pdf/others/"
            "Banning_Holiday_List_as_on_15102024.pdf")
OUTPUT_FILE = os.path.join(PROJECT_ROOT, "data", "gail_banning_list_144.csv")
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


def _download_pdf():
    os.makedirs(RAW_DIR, exist_ok=True)
    dest = os.path.join(RAW_DIR, "gail_banning_list_144.pdf")
    r = Fetcher.get(PDF_URL, timeout=60, retries=1, retry_delay=0, verify=False)
    body = r.body
    if isinstance(body, str):
        body = body.encode("utf-8", "replace")
    with open(dest, "wb") as f:
        f.write(body)
    return dest


def _parse_pdf(pdf_path, scraped_at):
    rows_out = []
    with pdfplumber.open(pdf_path) as pdf:
        for pno, page in enumerate(pdf.pages, start=1):
            for t in page.extract_tables() or []:
                if not t or len(t) < 2:
                    continue
                # Locate header row: must contain 'Name Of Company' in col 1.
                header_idx = None
                for i, r in enumerate(t):
                    if len(r) >= 2 and "name of company" in (str(r[1] or "")).lower():
                        header_idx = i
                        break
                if header_idx is None:
                    continue
                hdr = [_clean(c) for c in t[header_idx]]
                for r in t[header_idx + 1:]:
                    if not r or all(not _clean(c) for c in r):
                        continue
                    sno      = _clean(r[0]) if len(r) > 0 else ""
                    company  = _clean(r[1]) if len(r) > 1 else ""
                    eff_date = _clean(r[2]) if len(r) > 2 else ""
                    address  = _clean(r[3]) if len(r) > 3 else ""
                    email    = _clean(r[4]) if len(r) > 4 else ""
                    vendor   = _clean(r[5]) if len(r) > 5 else ""
                    pan      = _clean(r[6]) if len(r) > 6 else ""
                    gstn     = _clean(r[7]) if len(r) > 7 else ""
                    if not company:
                        continue
                    details_parts = []
                    if eff_date:
                        details_parts.append(f"Effective Date: {eff_date}")
                    if email:
                        details_parts.append(f"Email: {email}")
                    if vendor:
                        details_parts.append(f"Vendor Code: {vendor}")
                    if pan:
                        details_parts.append(f"PAN: {pan}")
                    if gstn:
                        details_parts.append(f"GSTN: {gstn}")
                    if sno:
                        details_parts.append(f"S.No: {sno}")
                    rows_out.append({
                        "source_agency": "GAIL",
                        "source_list": "Banning Holiday List",
                        "case_unit": vendor or pan or sno,
                        "name": company,
                        "father_name": "",
                        "date_of_birth": "",
                        "gender": "",
                        "address": address,
                        "reward_amount": "",
                        "details": " | ".join(details_parts),
                        "has_document": "Yes",
                        "document_url": PDF_URL,
                        "detail_page_url": LIST_URL,
                        "interpol_notice_id": "",
                        "link_kind": "gail_banned",
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
    print("GAIL Banning Holiday List scraper (#144)")
    print("=" * 60)
    pdf_path = _download_pdf()
    scraped_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    rows = _parse_pdf(pdf_path, scraped_at)
    if not rows:
        raise RuntimeError("GAIL: zero rows parsed")
    save_to_csv(rows, OUTPUT_FILE)
    return rows


if __name__ == "__main__":
    run()
