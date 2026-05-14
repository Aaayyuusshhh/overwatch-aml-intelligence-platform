"""
NABARD Blacklisted NGOs scraper (#80).

Source: https://www.nabard.org/auth/writereaddata/File/ngo-list.xlsx
(linked from https://www.nabard.org/BlacklistedNGOs.aspx as "Debarred NGOs")

XLSX download via Scrapling Fetcher; parsed with pandas. Columns:
  Sr. No. | Name | Address | Name of the CEO / MD |
  Registration No. & Date | Date of debarring

Mapped to 17-column schema with link_kind = 'nabard_blacklisted_ngo'.
Sanity check: refuse to write CSV if rows < 50 (file has had 150+
historically; a sharp drop signals NABARD changed the file).
"""

import csv
import os
import tempfile
from datetime import datetime

import pandas as pd
from scrapling import Fetcher

XLSX_URL = "https://www.nabard.org/auth/writereaddata/File/ngo-list.xlsx"
EXPECTED_MIN = 50

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUTPUT_FILE = os.path.join(PROJECT_ROOT, "data", "nabard_blacklisted_ngos.csv")

CSV_FIELDS = [
    "source_agency", "source_list", "case_unit",
    "name", "father_name", "date_of_birth", "gender",
    "address", "reward_amount", "details",
    "has_document", "document_url", "detail_page_url",
    "interpol_notice_id", "link_kind", "scraped_at",
    "enrichment_status",
]


def _clean(v):
    if v is None or (isinstance(v, float) and pd.isna(v)):
        return ""
    return str(v).replace("\xa0", " ").strip()


def download():
    print(f"Fetching {XLSX_URL}")
    resp = Fetcher.get(XLSX_URL, timeout=60, retries=1, retry_delay=0, verify=False)
    status = getattr(resp, "status", None) or getattr(resp, "status_code", None)
    if status is None or status >= 400:
        raise RuntimeError(f"NABARD xlsx HTTP {status}")
    body = getattr(resp, "body", None) or getattr(resp, "content", None)
    if isinstance(body, str):
        body = body.encode("utf-8", "replace")
    if not body or body[:2] != b"PK":
        raise RuntimeError("NABARD xlsx response is not a ZIP/XLSX (server returned HTML?)")
    return body


def parse(xlsx_bytes):
    tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".xlsx")
    try:
        tmp.write(xlsx_bytes)
        tmp.close()
        df = pd.read_excel(tmp.name)
    finally:
        os.unlink(tmp.name)

    print(f"Parsed XLSX: {len(df)} rows, cols={list(df.columns)}")
    scraped_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    out = []
    for _i, r in df.iterrows():
        name = _clean(r.get("Name"))
        if not name:
            continue
        addr = _clean(r.get("Address"))
        ceo = _clean(r.get("Name of the CEO / MD"))
        regn = _clean(r.get("Registration No. & Date"))
        debar_date = _clean(r.get("Date of debarring"))
        sr = _clean(r.get("Sr. No."))
        # Build details string preserving ancillary fields.
        details_parts = []
        if ceo: details_parts.append(f"CEO/MD: {ceo}")
        if regn: details_parts.append(f"Registration: {regn}")
        if debar_date: details_parts.append(f"Debarred: {debar_date}")
        out.append({
            "source_agency": "NABARD",
            "source_list": "Blacklisted/Debarred NGOs",
            "case_unit": sr,
            "name": name,
            "father_name": "",
            "date_of_birth": "",
            "gender": "",
            "address": addr,
            "reward_amount": "",
            "details": " | ".join(details_parts),
            "has_document": "Yes",
            "document_url": XLSX_URL,
            "detail_page_url": "https://www.nabard.org/BlacklistedNGOs.aspx",
            "interpol_notice_id": "",
            "link_kind": "nabard_blacklisted_ngo",
            "scraped_at": scraped_at,
            "enrichment_status": "none",
        })
    if len(out) < EXPECTED_MIN:
        raise RuntimeError(
            f"NABARD: extracted {len(out)} rows, below floor {EXPECTED_MIN}"
        )
    return out


def save_to_csv(records, out_path):
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    with open(out_path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=CSV_FIELDS)
        w.writeheader()
        for r in records:
            w.writerow({k: r.get(k, "") for k in CSV_FIELDS})
    print(f"Saved {len(records)} records to {out_path}")


def run():
    print("=" * 60)
    print("NABARD Blacklisted NGOs scraper (#80)")
    print("=" * 60)
    body = download()
    records = parse(body)
    save_to_csv(records, OUTPUT_FILE)
    print("Done.")


if __name__ == "__main__":
    run()
