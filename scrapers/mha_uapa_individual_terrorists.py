"""
MHA Individual Terrorists Under UAPA scraper.

Source PDF: https://www.mha.gov.in/sites/default/files/2024-10/Listof57terrorists_07032024.pdf
Local archive: data/raw/mha_uapa_listof57terrorists_07032024.pdf

The PDF has a two-column table (Sl. No., Name of the Terrorist) on three
pages. We use pdfplumber.extract_tables() to read it directly. Names are
stored verbatim including all '@'-separated aliases — downstream
screening can split on ' @ ' for fuzzy matching.

This scraper reads the LOCAL PDF only and does not re-fetch from MHA.
To refresh, manually re-download the PDF into data/raw/.
"""

import csv
import os
import re
from datetime import datetime

import pdfplumber

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PDF_PATH = os.path.join(PROJECT_ROOT, "data", "raw",
                        "mha_uapa_listof57terrorists_07032024.pdf")
PDF_URL = ("https://www.mha.gov.in/sites/default/files/2024-10/"
           "Listof57terrorists_07032024.pdf")
OUTPUT_FILE = os.path.join(PROJECT_ROOT, "data",
                           "mha_uapa_individual_terrorists.csv")

# Filename declares 57 entries. We hard-fail if extraction returns any
# other count rather than silently writing a wrong-sized CSV.
EXPECTED_COUNT = 57

CSV_FIELDS = [
    "source_agency", "source_list", "case_unit",
    "name", "father_name", "date_of_birth", "gender",
    "address", "reward_amount", "details",
    "has_document", "document_url", "detail_page_url",
    "interpol_notice_id", "link_kind", "scraped_at",
    "enrichment_status",
]

# A serial-number cell is "1", "1.", "57.", etc. Header rows
# (e.g. "Sl.\nNo.") fail this match and are skipped.
SR_NO_RE = re.compile(r"^\d+\.?$")


def clean_name(value):
    """
    Three-rule cleanup for a raw extracted name cell:
      1. Collapse internal '\\n' (line-wrap artifacts) to spaces.
      2. Squeeze runs of whitespace to single spaces.
      3. Strip trailing stray punctuation seen on a few PDF entries
         (period, right-double-quote U+201D, straight quote, space).
    """
    if value is None:
        return ""
    s = value.replace("\n", " ")
    s = re.sub(r"\s+", " ", s).strip()
    s = s.rstrip(".”\" ")
    return s


def parse_rows_from_pdf(pdf_path):
    """Iterate every page/table/row and return a list of cleaned name strings.

    Page 1's table is 4 columns wide (the header drives the layout and a few
    name cells sit in cells[2] rather than cells[1] due to a pdfplumber
    alignment quirk). We therefore concatenate every non-empty cell from
    index 1 onward to reconstruct the name, which works on both the 4-col
    page 1 and the 2-col pages 2-3.
    """
    names = []
    with pdfplumber.open(pdf_path) as pdf:
        for page in pdf.pages:
            for table in page.extract_tables():
                for row in table:
                    if not row or len(row) < 2:
                        continue
                    sr_no_cell = (row[0] or "").strip()
                    if not SR_NO_RE.match(sr_no_cell):
                        # header / continuation / artifact row — skip
                        continue
                    name_parts = [c for c in row[1:] if c]
                    name = clean_name(" ".join(name_parts))
                    if name:
                        names.append(name)
    return names


def build_records(names):
    """Wrap each cleaned name in a 17-column schema record."""
    scraped_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    out = []
    for n in names:
        out.append({
            "source_agency": "MHA",
            "source_list": "Individual Terrorists Under UAPA",
            "case_unit": "",
            "name": n,
            "father_name": "",
            "date_of_birth": "",
            "gender": "",
            "address": "",
            "reward_amount": "",
            "details": "",
            "has_document": "Yes",
            "document_url": PDF_URL,
            "detail_page_url": "",
            "interpol_notice_id": "",
            "link_kind": "mha_uapa_individual",
            "scraped_at": scraped_at,
            "enrichment_status": "none",
        })
    return out


def save_to_csv(records, output_file):
    """Write records to CSV. Creates parent dirs if needed."""
    if not records:
        print("No records to save.")
        return
    os.makedirs(os.path.dirname(output_file), exist_ok=True)
    with open(output_file, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=CSV_FIELDS)
        writer.writeheader()
        for row in records:
            writer.writerow({k: row.get(k, "") for k in CSV_FIELDS})
    print(f"Saved {len(records)} records to {output_file}")


def run():
    print("=" * 60)
    print("MHA Individual Terrorists Under UAPA scraper")
    print("=" * 60)
    print(f"Reading {PDF_PATH}")

    if not os.path.exists(PDF_PATH):
        raise FileNotFoundError(
            f"Local PDF not found: {PDF_PATH}\n"
            f"Download from {PDF_URL} into data/raw/ and re-run."
        )

    names = parse_rows_from_pdf(PDF_PATH)
    print(f"Extracted {len(names)} names from PDF")

    if len(names) != EXPECTED_COUNT:
        raise RuntimeError(
            f"Sanity check failed: extracted {len(names)} names but the "
            f"source declares {EXPECTED_COUNT} ('Listof57' in filename). "
            f"NOT writing CSV. Inspect the PDF and parser."
        )

    records = build_records(names)
    print("-" * 60)
    save_to_csv(records, OUTPUT_FILE)
    print("Done.")


if __name__ == "__main__":
    run()
