"""
RBI List of Cancelled NBFCs (#105).

Source PDF: data/rbi_cancelled_nbfcs.pdf
(consolidated list of NBFCs and ARCs whose Certificate of Registration
has been cancelled by the RBI, as on March 31, 2026).

Listing page: https://rbi.org.in/Scripts/bs_nbfclist.aspx

Per-page table with 4 columns:
  Sl. No. | Name of the company | Regional Office | Address

The task brief mentioned CoR no / issue / cancellation dates, but the
published PDF only carries the four columns above. We map Regional
Office into details and the registered-office address into the
address column.
"""

import csv
import os
import re
from datetime import datetime

import pdfplumber

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PDF_PATH = os.path.join(PROJECT_ROOT, "data", "rbi_cancelled_nbfcs.pdf")
OUTPUT_FILE = os.path.join(PROJECT_ROOT, "data",
                            "rbi_cancelled_nbfcs_105.csv")
DOC_URL = "https://rbi.org.in/Scripts/bs_nbfclist.aspx"

CSV_FIELDS = [
    "source_agency", "source_list", "case_unit",
    "name", "father_name", "date_of_birth", "gender",
    "address", "reward_amount", "details",
    "has_document", "document_url", "detail_page_url",
    "interpol_notice_id", "link_kind", "scraped_at",
    "enrichment_status",
]

SN_RE = re.compile(r"^\d+$")


def _clean(s):
    if s is None:
        return ""
    return re.sub(r"\s+", " ", str(s).replace("\xa0", " ")).strip()


def scrape():
    if not os.path.exists(PDF_PATH):
        raise RuntimeError(f"RBI Cancelled NBFCs: PDF missing at {PDF_PATH}")
    scraped_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    out = []
    seen = set()
    with pdfplumber.open(PDF_PATH) as pdf:
        n_pages = len(pdf.pages)
        for page in pdf.pages:
            for table in page.extract_tables() or []:
                for row in table:
                    cells = [_clean(c) for c in (row + [""] * 4)[:4]]
                    sl, name, ro, address = cells
                    # Skip title / header / blank rows
                    if not SN_RE.match(sl):
                        continue
                    if not name:
                        continue
                    key = (sl, name.lower())
                    if key in seen:
                        continue
                    seen.add(key)
                    out.append({
                        "source_agency": "Reserve Bank of India (RBI)",
                        "source_list":   "List of Cancelled NBFCs",
                        "case_unit":     sl,
                        "name":          name,
                        "father_name":   "",
                        "date_of_birth": "",
                        "gender":        "",
                        "address":       address,
                        "reward_amount": "",
                        "details":       (f"Sl No: {sl} | Regional Office: {ro}"
                                          if ro else f"Sl No: {sl}"),
                        "has_document":  "Yes",
                        "document_url":  DOC_URL,
                        "detail_page_url": DOC_URL,
                        "interpol_notice_id": "",
                        "link_kind":     "manual_discovery",
                        "scraped_at":    scraped_at,
                        "enrichment_status": "",
                    })
    print(f"  parsed {len(out)} NBFC rows from {n_pages} pages")
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
    print("RBI List of Cancelled NBFCs (#105)")
    print("=" * 60)
    rows = scrape()
    if not rows:
        raise RuntimeError("RBI Cancelled NBFCs: 0 rows parsed")
    save_to_csv(rows, OUTPUT_FILE)
    return rows


if __name__ == "__main__":
    run()
