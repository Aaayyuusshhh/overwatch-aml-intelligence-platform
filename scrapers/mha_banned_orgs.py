"""
MHA Banned Organisations scraper.

Source: https://www.mha.gov.in/en/banned-organisations

Single static HTML table with two columns (Sl-No, Organisation). Every
data row is plain text — no anchors, no PDFs, no per-record detail pages.
We treat each row as one record and store the organisation name in `name`.

This is the first non-CBI source in the pipeline. link_kind is the new
constant 'mha_banned_org' for every row. Person-shaped fields
(date_of_birth, father_name, address, gender, etc.) are all empty since
these rows describe entities, not individuals — `source_list` is what
disambiguates org rows from person rows downstream.

Note: classify_link() / absolute_url() from the Red/Yellow Notices
template were dropped here because every row's link_kind is constant and
no anchors need URL classification. If we later extract shared helpers
into common/, we'll pull them from the CBI scrapers that still use them.
"""

import csv
import os
from datetime import datetime

from scrapling import Fetcher

LIST_URL = "https://www.mha.gov.in/en/banned-organisations"

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUTPUT_FILE = os.path.join(PROJECT_ROOT, "data", "mha_banned_orgs.csv")

CSV_FIELDS = [
    "source_agency", "source_list", "case_unit",
    "name", "father_name", "date_of_birth", "gender",
    "address", "reward_amount", "details",
    "has_document", "document_url", "detail_page_url",
    "interpol_notice_id", "link_kind", "scraped_at",
    "enrichment_status",
]

MISSING_VARIANTS = {"-", "--", "n/a", "na", "unknown", "none"}


# ============================================================
# SMALL HELPERS
# ============================================================
def clean_missing(value):
    """Collapse known 'missing' placeholders to ''."""
    if value is None:
        return ""
    s = str(value).strip()
    if s.lower() in MISSING_VARIANTS or s == "":
        return ""
    return s


# ============================================================
# ROW PARSING
# ============================================================
def parse_row(row, scraped_at):
    """
    Build a record dict from one <tr>. Returns None if unusable.

    The MHA Banned Orgs table has plain text in cells[1] (the org name)
    — no anchor tag, no link, no PDF — so we read .text directly rather
    than going through `cells[1].find('a')` like the CBI scrapers do.
    """
    cells = row.find_all("td")
    if len(cells) < 2:
        return None
    name = clean_missing(cells[1].text)
    if not name:
        return None

    return {
        "source_agency": "MHA",
        "source_list": "Banned Organizations",
        "case_unit": "",
        "name": name,
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
        "link_kind": "mha_banned_org",
        "scraped_at": scraped_at,
        "enrichment_status": "none",
    }


# ============================================================
# SCRAPE + SAVE
# ============================================================
def scrape_banned_orgs():
    """Fetch the list page once and parse every table row into a record."""
    print(f"Fetching {LIST_URL}")
    page = Fetcher.get(LIST_URL)

    # MHA's table has no class attribute; the first <table> on the page
    # is the data table. The class-prefixed lookup is kept only as a
    # defensive try in case the markup ever picks one up.
    table = page.find("table.commonTable") or page.find("table")
    if table is None:
        print("ERROR: no table found on page")
        return []

    rows = table.find_all("tr")
    print(f"Total <tr>: {len(rows)} (incl. header)")

    scraped_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    records = []
    skipped = 0
    for i, row in enumerate(rows[1:], start=1):
        rec = parse_row(row, scraped_at)
        if rec is None:
            print(f"  [{i}] skipped (could not parse)")
            skipped += 1
            continue
        records.append(rec)
    print(f"Parsed: {len(records)}  Skipped: {skipped}")
    return records


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


# ============================================================
# MAIN
# ============================================================
def run():
    print("=" * 60)
    print("MHA Banned Organisations scraper")
    print("=" * 60)
    records = scrape_banned_orgs()
    print("-" * 60)
    save_to_csv(records, OUTPUT_FILE)
    print("Done.")


if __name__ == "__main__":
    run()
