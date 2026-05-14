"""
CBI Interpol Red Notices scraper.

The page at https://cbi.gov.in/interpol-red-notice publishes a single
HTML table. Unlike Yellow Notices, this source is homogeneous: every
row's link points to interpol.int — there are no CBI-hosted documents
in this list. Every record therefore has link_kind="interpol_notice"
with the notice ID captured from the URL fragment into
`interpol_notice_id`.

The shared parse_row()/classify_link() logic still recognises the
`cbi_document` branch (the same code is used by Yellow Notices), but
that branch is never exercised on this source.
"""

import csv
import os
from datetime import datetime

from scrapling import Fetcher

LIST_URL = "https://cbi.gov.in/interpol-red-notice"
BASE_URL = "https://cbi.gov.in"

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUTPUT_FILE = os.path.join(PROJECT_ROOT, "data", "cbi_red_notices.csv")

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


def absolute_url(href):
    """Turn a relative href into a full URL on cbi.gov.in. External URLs pass through."""
    if not href:
        return ""
    if href.startswith("http"):
        return href
    if not href.startswith("/"):
        href = "/" + href
    return BASE_URL + href


def classify_link(href):
    """Return (link_kind, interpol_notice_id) for a row's href."""
    if not href:
        return "", ""
    if "interpol.int" in href:
        notice_id = href.split("#", 1)[1].strip() if "#" in href else ""
        return "interpol_notice", notice_id
    if "/assets/files/wanted/" in href.lower():
        return "cbi_document", ""
    return "", ""


# ============================================================
# ROW PARSING
# ============================================================
def parse_row(row, scraped_at):
    """Build a record dict for one <tr>, or None if the row is unusable."""
    cells = row.find_all("td")
    if len(cells) < 2:
        return None
    a = cells[1].find("a")
    if a is None:
        return None
    href = a.attrib.get("href", "")
    name = clean_missing(a.text)
    if not name:
        return None

    link_kind, interpol_notice_id = classify_link(href)
    target = absolute_url(href)

    return {
        "source_agency": "CBI",
        "source_list": "Interpol Red Notice",
        "case_unit": "",
        "name": name,
        "father_name": "",
        "date_of_birth": "",
        "gender": "",
        "address": "",
        "reward_amount": "",
        "details": "",
        "has_document": "Yes" if link_kind == "cbi_document" else "No",
        "document_url": target if link_kind == "cbi_document" else "",
        "detail_page_url": target,
        "interpol_notice_id": interpol_notice_id,
        "link_kind": link_kind,
        "scraped_at": scraped_at,
        "enrichment_status": "none",
    }


# ============================================================
# SCRAPE + SAVE
# ============================================================
def scrape_yellow_notices():
    """Fetch the list page once and parse every table row into a record."""
    print(f"Fetching {LIST_URL}")
    page = Fetcher.get(LIST_URL)

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
        if rec["link_kind"] == "":
            print(f"  [{i}] WARNING unrecognised href for {rec['name']!r}: {rec['detail_page_url']!r}")
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
    print("CBI Interpol Red Notices scraper")
    print("=" * 60)
    records = scrape_yellow_notices()
    print("-" * 60)
    save_to_csv(records, OUTPUT_FILE)
    print("Done.")


if __name__ == "__main__":
    run()
