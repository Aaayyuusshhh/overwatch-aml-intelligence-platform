"""
NIA Arrested Persons In Custody scraper (#99).

Source: https://nia.gov.in/arrested-persons-in-custody-of-nia

This is a Drupal 'view' page that lists individuals currently in NIA
custody. As of build time the public page renders the empty marker
'No Any Arrested Persons In Custody of NIA' inside a div.view-empty -
i.e. the agency reports zero people currently in custody.

This scraper is honest about that state: when the empty marker is
present it writes a CSV with zero data rows (header only). When the
view is populated the scraper extracts each row from the standard
Drupal views-row container. Either outcome is a valid scrape.

link_kind = 'nia_arrested_in_custody'.
"""

import csv
import os
from datetime import datetime

from scrapling import Fetcher

LIST_URL = "https://nia.gov.in/arrested-persons-in-custody-of-nia"
EMPTY_MARKER = "No Any Arrested Persons In Custody"

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUTPUT_FILE = os.path.join(PROJECT_ROOT, "data", "nia_arrested_persons.csv")

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


def _row_to_record(node, scraped_at):
    """Best-effort field extraction from a Drupal views-row node."""
    text = _clean(node.get_all_text() if hasattr(node, "get_all_text") else node.text)
    if not text:
        return None
    return {
        "source_agency": "NIA",
        "source_list": "Arrested Persons In Custody",
        "case_unit": "",
        "name": text.split("\n", 1)[0][:200].strip(),
        "father_name": "",
        "date_of_birth": "",
        "gender": "",
        "address": "",
        "reward_amount": "",
        "details": text[:2000],
        "has_document": "No",
        "document_url": "",
        "detail_page_url": LIST_URL,
        "interpol_notice_id": "",
        "link_kind": "nia_arrested_in_custody",
        "scraped_at": scraped_at,
        "enrichment_status": "none",
    }


def scrape():
    print(f"Fetching {LIST_URL}")
    page = Fetcher.get(LIST_URL, timeout=30, retries=1, retry_delay=0, verify=False)
    status = getattr(page, "status", None) or getattr(page, "status_code", None)
    if status is None or status >= 400:
        raise RuntimeError(f"NIA arrested HTTP {status}")

    # Empty-state branch: Drupal view-empty container is the source of
    # truth. If present we honour it (zero rows, no error).
    body = page.body
    if isinstance(body, bytes):
        body = body.decode("utf-8", "replace")
    if EMPTY_MARKER.lower() in body.lower():
        print(f"NIA reports empty list ('{EMPTY_MARKER}'). Writing 0 records.")
        return []

    # Populated state: try standard Drupal views-row / view-content / row
    # containers in priority order. We are tolerant about which one the
    # NIA Drupal theme uses.
    scraped_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    for selector in ("div.views-row", "div.view-content > div",
                     "article", "div.row > div.col-md-4"):
        try:
            nodes = page.find_all(selector) or []
        except Exception:
            nodes = []
        if len(nodes) >= 1:
            print(f"Matched {len(nodes)} nodes via selector {selector!r}")
            out = []
            for n in nodes:
                rec = _row_to_record(n, scraped_at)
                if rec and rec["name"]:
                    out.append(rec)
            if out:
                return out

    raise RuntimeError(
        "NIA arrested page is non-empty (no view-empty marker) but no known "
        "view-row selector matched. Layout may have changed - investigate."
    )


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
    print("NIA Arrested Persons In Custody scraper (#99)")
    print("=" * 60)
    records = scrape()
    save_to_csv(records, OUTPUT_FILE)
    print("Done.")


if __name__ == "__main__":
    run()
