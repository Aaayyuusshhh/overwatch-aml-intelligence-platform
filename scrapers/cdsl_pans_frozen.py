"""
CDSL — List of PANs Frozen (#163).

Source: data/cdsl_pans_frozen.pdf (155 pages, manually downloaded).

Per-page table with six columns:
  Client Name | Client PAN | Subject line | Reference Number |
  Date of Deactivation | Remarks

pdfplumber.extract_tables() returns the 6 columns cleanly on most
pages. Multi-line cells (long names / long subject lines) are
preserved with embedded "\\n", which we collapse to a single space.
A handful of page-boundary rows lose the leading Name/PAN cells —
the row text still contains the PAN as a 10-char alphanumeric inside
some other cell; we recover those with a regex sweep over the row
text and skip the row only when neither approach finds anything.
"""

import csv
import os
import re
from datetime import datetime

import pdfplumber

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PDF_PATH = os.path.join(PROJECT_ROOT, "data", "cdsl_pans_frozen.pdf")
OUTPUT_FILE = os.path.join(PROJECT_ROOT, "data", "cdsl_pans_frozen_163.csv")

CSV_FIELDS = [
    "source_agency", "source_list", "case_unit",
    "name", "father_name", "date_of_birth", "gender",
    "address", "reward_amount", "details",
    "has_document", "document_url", "detail_page_url",
    "interpol_notice_id", "link_kind", "scraped_at",
    "enrichment_status",
]

PAN_RE = re.compile(r"\b[A-Z]{5}\d{4}[A-Z]\b")


def _clean(s):
    if s is None:
        return ""
    return re.sub(r"\s+", " ", str(s).replace("\xa0", " ")).strip(" .,;-")


def scrape():
    if not os.path.exists(PDF_PATH):
        raise RuntimeError(f"CDSL PANs Frozen: PDF missing at {PDF_PATH}")
    scraped_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    out = []
    skipped = 0
    seen = set()
    with pdfplumber.open(PDF_PATH) as pdf:
        for page in pdf.pages:
            for table in page.extract_tables() or []:
                for row in table:
                    cells = [_clean(c) for c in (row + [""] * 6)[:6]]
                    name, pan, subject, ref_no, date_dea, remarks = cells
                    # Skip header
                    if name.lower() == "client name":
                        continue
                    # If table column boundaries collapsed and we have a
                    # 4-cell row, look at all cells for a PAN, infer the
                    # rest from position.
                    if not pan:
                        joined = " ".join(cells)
                        m = PAN_RE.search(joined)
                        pan = m.group(0) if m else ""
                    if not name and not pan:
                        skipped += 1
                        continue
                    # Drop blank-name rows (continuation artefacts)
                    if not name:
                        skipped += 1
                        continue
                    # Dedup by (name, pan, ref_no) tuple — the PDF
                    # sometimes repeats the same row when a record
                    # straddles a page.
                    key = (name.lower(), pan, ref_no)
                    if key in seen:
                        continue
                    seen.add(key)
                    detail_parts = []
                    if subject:   detail_parts.append(f"Subject: {subject}")
                    if ref_no:    detail_parts.append(f"Ref: {ref_no}")
                    if date_dea:  detail_parts.append(f"Date of Deactivation: {date_dea}")
                    if remarks:   detail_parts.append(f"Remarks: {remarks}")
                    out.append({
                        "source_agency": "Central Depository Services (India) Limited (CDSL)",
                        "source_list":   "List of PANs Frozen on Account of Non-Delivery of SCN/Orders",
                        "case_unit":     pan,
                        "name":          name,
                        "father_name":   "",
                        "date_of_birth": "",
                        "gender":        "",
                        "address":       "",
                        "reward_amount": "",
                        "details":       " | ".join(detail_parts),
                        "has_document":  "Yes",
                        "document_url":  "",
                        "detail_page_url": "",
                        "interpol_notice_id": "",
                        "link_kind":     "manual_discovery",
                        "scraped_at":    scraped_at,
                        "enrichment_status": "",
                    })
    print(f"  parsed {len(out)} records (skipped {skipped} continuation rows)")
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
    print("CDSL List of PANs Frozen (#163)")
    print("=" * 60)
    rows = scrape()
    if not rows:
        raise RuntimeError("CDSL PANs: 0 rows parsed")
    save_to_csv(rows, OUTPUT_FILE)
    return rows


if __name__ == "__main__":
    run()
