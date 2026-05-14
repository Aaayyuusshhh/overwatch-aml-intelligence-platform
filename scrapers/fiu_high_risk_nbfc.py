"""
FIU High Risk NBFCs (#29).

Source: a local 139-page PDF at data/High Risk NBFCs updated.pdf,
manually downloaded from
  https://fiuindia.gov.in/pdfs/quicklinks/High%20Risk%20NBFCs%20updated.pdf

The PDF lists ~9,200 NBFCs that FIU-IND categorised as "High Risk
Financial Institutions" on 27-02-2018 for non-registration of their
Principal Officer under PMLA / PML Rules.

Table structure is uniform: two columns, "Sr. No. | Company Name",
spanning every page. pdfplumber.extract_tables() recovers the rows
cleanly. The only chrome to skip is the title block on page 1
("List of NBFCs categorized as 'High Risk Financial Institutions'…")
and the column header row.
"""

import csv
import os
import re
from datetime import datetime

import pdfplumber

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PDF_PATH = os.path.join(PROJECT_ROOT, "data", "High Risk NBFCs updated.pdf")
OUTPUT_FILE = os.path.join(PROJECT_ROOT, "data", "fiu_high_risk_nbfc_29.csv")
DOC_URL = "https://fiuindia.gov.in/pdfs/quicklinks/High%20Risk%20NBFCs%20updated.pdf"

CSV_FIELDS = [
    "source_agency", "source_list", "case_unit",
    "name", "father_name", "date_of_birth", "gender",
    "address", "reward_amount", "details",
    "has_document", "document_url", "detail_page_url",
    "interpol_notice_id", "link_kind", "scraped_at",
    "enrichment_status",
]

DETAILS = (
    "Categorized as High Risk by FIU-IND | "
    "Non-registration of Principal Officer | Date: 27-02-2018"
)

SN_RE = re.compile(r"^\d+$")


def _clean(s):
    if s is None:
        return ""
    return re.sub(r"\s+", " ", str(s).replace("\xa0", " ")).strip()


def _extract():
    """Yield (sr_no, company_name) for every data row in the PDF.

    Skips:
      - the page-1 title block (1-cell row with the descriptive text)
      - the "Sr. No. | Company Name" header row
      - any row whose first cell isn't a pure integer
    """
    with pdfplumber.open(PDF_PATH) as pdf:
        n_pages = len(pdf.pages)
        for page in pdf.pages:
            for t in page.extract_tables() or []:
                for row in t:
                    if not row:
                        continue
                    cells = [_clean(c) for c in row]
                    if len(cells) < 2:
                        continue
                    sr, name = cells[0], cells[1]
                    if not SN_RE.match(sr):
                        continue
                    if not name:
                        continue
                    yield sr, name
        return n_pages


def scrape():
    if not os.path.exists(PDF_PATH):
        raise RuntimeError(f"FIU High Risk: PDF missing at {PDF_PATH}")
    scraped_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    out = []
    for sr, name in _extract():
        out.append({
            "source_agency": "Financial Intelligence Unit (FIU)",
            "source_list": "NBFCs Categorized as High Risk Financial Institutions",
            "case_unit": sr,
            "name": name,
            "father_name": "",
            "date_of_birth": "",
            "gender": "",
            "address": "",
            "reward_amount": "",
            "details": DETAILS,
            "has_document": "Yes",
            "document_url": DOC_URL,
            "detail_page_url": "",
            "interpol_notice_id": "",
            "link_kind": "manual_discovery",
            "scraped_at": scraped_at,
            "enrichment_status": "",
        })
    print(f"  parsed {len(out)} NBFC rows from the PDF")
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
    print("FIU High Risk NBFCs (#29)")
    print("=" * 60)
    rows = scrape()
    save_to_csv(rows, OUTPUT_FILE)
    return rows


if __name__ == "__main__":
    run()
