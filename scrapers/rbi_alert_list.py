"""
RBI Alert List scraper (#104).

Source: https://rbi.org.in/scripts/bs_viewcontent.aspx?Id=4235
Page contains 3 <table>s. The third (index 2) is the actual alert list
with three columns: Sr. No / Name / Website. Currently 95 entities.

Output: data/rbi_alert_list.csv with the 17-column shared schema.
link_kind = 'rbi_alert_list'.

Sanity check: refuse to write CSV if extracted row count drops below
the floor (currently 50) - a sharp drop usually means RBI changed the
table layout, which we want to know about loudly.
"""

import csv
import os
from datetime import datetime

from scrapling import Fetcher

LIST_URL = "https://rbi.org.in/scripts/bs_viewcontent.aspx?Id=4235"
EXPECTED_MIN = 50

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUTPUT_FILE = os.path.join(PROJECT_ROOT, "data", "rbi_alert_list.csv")

CSV_FIELDS = [
    "source_agency", "source_list", "case_unit",
    "name", "father_name", "date_of_birth", "gender",
    "address", "reward_amount", "details",
    "has_document", "document_url", "detail_page_url",
    "interpol_notice_id", "link_kind", "scraped_at",
    "enrichment_status",
]


def _clean(s):
    return (s or "").replace("\xa0", " ").strip()


def find_alert_table(page):
    """Return the *innermost* table whose header row is exactly
    Sr. No / Name / Website. RBI's page has one wrapper table that
    contains the same headers nested - we must match the strict
    3-cell header to skip the wrapper."""
    for t in (page.find_all("table") or []):
        rows = t.find_all("tr") or []
        if not rows:
            continue
        first_cells = (rows[0].find_all("td") or rows[0].find_all("th") or [])
        if len(first_cells) != 3:
            continue
        headers = [_clean(c.text).lower() for c in first_cells]
        if headers == ["sr. no", "name", "website"]:
            return t
    return None


def scrape():
    print(f"Fetching {LIST_URL}")
    page = Fetcher.get(LIST_URL, timeout=30, retries=1, retry_delay=0, verify=False)
    status = getattr(page, "status", None) or getattr(page, "status_code", None)
    if status is None or status >= 400:
        raise RuntimeError(f"RBI alert list HTTP {status}")

    table = find_alert_table(page)
    if table is None:
        raise RuntimeError("RBI alert list: target table not found "
                           "(expected headers Sr. No / Name / Website)")

    rows = table.find_all("tr") or []
    print(f"Found target table with {len(rows)} <tr> (incl. header)")

    scraped_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    out = []
    for i, tr in enumerate(rows[1:], start=1):
        cells = tr.find_all("td") or []
        if len(cells) < 3:
            continue
        sr = _clean(cells[0].text)
        name = _clean(cells[1].text)
        website = _clean(cells[2].text)
        if not name:
            continue
        out.append({
            "source_agency": "RBI",
            "source_list": "Alert List",
            "case_unit": sr,
            "name": name,
            "father_name": "",
            "date_of_birth": "",
            "gender": "",
            "address": "",
            "reward_amount": "",
            "details": f"Unauthorised forex trading platform / app",
            "has_document": "No",
            "document_url": "",
            "detail_page_url": website,
            "interpol_notice_id": "",
            "link_kind": "rbi_alert_list",
            "scraped_at": scraped_at,
            "enrichment_status": "none",
        })
    if len(out) < EXPECTED_MIN:
        raise RuntimeError(
            f"RBI alert list extracted {len(out)} rows, below floor {EXPECTED_MIN} "
            "- refusing to write CSV (RBI likely changed layout)"
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
    print("RBI Alert List scraper (#104)")
    print("=" * 60)
    records = scrape()
    save_to_csv(records, OUTPUT_FILE)
    print("Done.")


if __name__ == "__main__":
    run()
