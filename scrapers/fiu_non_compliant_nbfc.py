"""
FIU Non-Compliant NBFC List (#30).

Source: a local 104-page PDF at data/NonCompliantNBFC28022026.pdf,
manually downloaded from
  https://fiuindia.gov.in/pdfs/downloads/NonCompliantNBFC28022026.pdf
The page is WAF-blocked to scripted fetches (BIG-IP ASM "Request
Rejected"), so the file is pre-staged on disk.

The PDF is text-based (extract_text() returns full text) and
pdfplumber.extract_tables() recovers the table structure cleanly. The
table has 7 columns:
  Sr No | NBFC Name | Regional Office | Corporate Identification Number | Layer | Address | Email ID

Two sections appear in sequence:
  - "List of non-compliant Middle Layer NBFCs"
  - "List of non-compliant Base Layer NBFCs"

Per-cell content can span multiple visual lines; pdfplumber returns
those as embedded "\n" characters inside the cell — we collapse them
to a single space.
"""

import csv
import os
import re
from datetime import datetime

import pdfplumber

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PDF_PATH = os.path.join(PROJECT_ROOT, "data", "NonCompliantNBFC28022026.pdf")
OUTPUT_FILE = os.path.join(PROJECT_ROOT, "data", "fiu_non_compliant_nbfc_30.csv")
DOC_URL = "https://fiuindia.gov.in/pdfs/downloads/NonCompliantNBFC28022026.pdf"

CSV_FIELDS = [
    "source_agency", "source_list", "case_unit",
    "name", "father_name", "date_of_birth", "gender",
    "address", "reward_amount", "details",
    "has_document", "document_url", "detail_page_url",
    "interpol_notice_id", "link_kind", "scraped_at",
    "enrichment_status",
]

HEADER_ROW_MARKERS = {"sr no", "sr no.", "s no", "s no."}
SECTION_HEADERS = {
    "list of non-compliant middle layer nbfcs",
    "list of non-compliant base layer nbfcs",
}


def _clean(s):
    if s is None:
        return ""
    return re.sub(r"\s+", " ", str(s).replace("\xa0", " ")).strip()


def _is_header_row(row):
    """Return True if this row is the header repeating on each page."""
    first = (_clean(row[0]) if row else "").lower()
    return first in HEADER_ROW_MARKERS


def _is_section_header_row(row):
    """Return (True, section_label) when row carries a section title."""
    joined = " ".join(_clean(c) for c in row if c).lower()
    for marker in SECTION_HEADERS:
        if marker in joined:
            if "middle layer" in marker:
                return True, "Middle"
            return True, "Base"
    return False, None


def _layer_from_row(row, current_layer):
    """Cell index 4 carries the layer label ('Middle' / 'Base')."""
    if len(row) > 4 and _clean(row[4]).lower() in {"middle", "base", "upper"}:
        return _clean(row[4]).title()
    return current_layer


def _extract_rows():
    rows = []
    current_layer = "Middle"  # PDF starts with Middle section
    with pdfplumber.open(PDF_PATH) as pdf:
        n_pages = len(pdf.pages)
        for pno, page in enumerate(pdf.pages, start=1):
            tables = page.extract_tables() or []
            if not tables:
                continue
            for t in tables:
                for raw_row in t:
                    if not raw_row:
                        continue
                    is_section, label = _is_section_header_row(raw_row)
                    if is_section:
                        current_layer = label
                        continue
                    if _is_header_row(raw_row):
                        continue
                    cells = [_clean(c) for c in raw_row]
                    if len(cells) < 7:
                        cells = cells + [""] * (7 - len(cells))
                    sr_no, name, ro, cin, layer, address, email = cells[:7]
                    if not sr_no.strip() and not name.strip():
                        continue
                    # Sr No should be a digit; if it's blank/non-numeric and
                    # the row carries no NBFC name either, skip.
                    if not name:
                        continue
                    current_layer = _layer_from_row(raw_row, current_layer)
                    rows.append({
                        "sr_no": sr_no,
                        "name": name,
                        "regional_office": ro,
                        "cin": cin,
                        "layer": layer or current_layer,
                        "address": address,
                        "email": email,
                    })
        return rows, n_pages


def _to_record(r, scraped_at):
    parts = []
    if r["cin"]:
        parts.append(f"CIN: {r['cin']}")
    if r["layer"]:
        parts.append(f"Layer: {r['layer']}")
    if r["regional_office"]:
        parts.append(f"Regional Office: {r['regional_office']}")
    if r["email"]:
        parts.append(f"Email: {r['email']}")
    return {
        "source_agency": "Financial Intelligence Unit (FIU)",
        "source_list": "Non Compliant NBFC List",
        "case_unit": "",
        "name": r["name"],
        "father_name": "",
        "date_of_birth": "",
        "gender": "",
        "address": r["address"],
        "reward_amount": "",
        "details": " | ".join(parts),
        "has_document": "Yes",
        "document_url": DOC_URL,
        "detail_page_url": "",
        "interpol_notice_id": "",
        "link_kind": "manual_discovery",
        "scraped_at": scraped_at,
        "enrichment_status": "",
    }


def scrape():
    if not os.path.exists(PDF_PATH):
        raise RuntimeError(f"FIU NBFC: PDF missing at {PDF_PATH}")
    raws, n_pages = _extract_rows()
    print(f"  parsed {n_pages} pages -> {len(raws)} raw rows")
    scraped_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    out = [_to_record(r, scraped_at) for r in raws]
    return out


def save_to_csv(rows, path):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=CSV_FIELDS)
        w.writeheader()
        for r in rows:
            w.writerow({k: r.get(k, "") for k in CSV_FIELDS})
    print(f"Saved {len(rows)} records to {path}")


def run():
    print("=" * 60)
    print("FIU Non-Compliant NBFC List (#30)")
    print("=" * 60)
    rows = scrape()
    save_to_csv(rows, OUTPUT_FILE)
    return rows


if __name__ == "__main__":
    run()
