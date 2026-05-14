"""
NHB Companies Not Valid for Acceptance of Public Deposits (#90).

Source: https://www.nhb.org.in/companies-whose-application-for-cor-have-been-cancelled/
Discovered via URL discovery sweep — the page title is
"Companies whose CoR has been cancelled", which is the same concept
as the PPT entry "Companies Not Valid for Acceptance of Public
Deposits": these are housing finance companies whose Certificate of
Registration (CoR) under the NHB Act was cancelled and so cannot
legally accept public deposits.

The page has one HTML table:
  Sl. | Name of the Company | Address
"""

import csv
import os
import re
from datetime import datetime

from bs4 import BeautifulSoup
from scrapling import Fetcher

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SOURCE_URL = (
    "https://www.nhb.org.in/companies-whose-application-for-cor-have-been-cancelled/"
)
OUTPUT_FILE = os.path.join(PROJECT_ROOT, "data", "nhb_companies_not_valid_90.csv")

CSV_FIELDS = [
    "source_agency", "source_list", "case_unit",
    "name", "father_name", "date_of_birth", "gender",
    "address", "reward_amount", "details",
    "has_document", "document_url", "detail_page_url",
    "interpol_notice_id", "link_kind", "scraped_at",
    "enrichment_status",
]


def _clean(s):
    if s is None:
        return ""
    s = str(s).replace("\xa0", " ").replace("–", "-")
    return re.sub(r"\s+", " ", s).strip()


def _fetch_html():
    r = Fetcher.get(SOURCE_URL, timeout=60, retries=2, retry_delay=2, verify=False)
    body = getattr(r, "body", None) or getattr(r, "content", None)
    if isinstance(body, bytes):
        body = body.decode("utf-8", "ignore")
    if not body or "Cancelled" not in body:
        raise RuntimeError("NHB: page body missing expected content")
    return body


def _parse_rows(html):
    soup = BeautifulSoup(html, "html.parser")
    table = soup.find("table")
    if table is None:
        raise RuntimeError("NHB: no <table> on page")

    rows = []
    for tr in table.find_all("tr"):
        cells = [c.get_text(" ", strip=True) for c in tr.find_all(["td", "th"])]
        if len(cells) < 3:
            continue
        sl, name, address = cells[0], cells[1], cells[2]
        if not _clean(name) or _clean(name).lower() in {"name of the company"}:
            continue
        if not re.match(r"^\d+$", _clean(sl)):
            continue
        rows.append((_clean(sl), _clean(name), _clean(address)))
    return rows


def scrape():
    html = _fetch_html()
    rows = _parse_rows(html)
    if not rows:
        raise RuntimeError("NHB: 0 rows parsed from table")
    scraped_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    out = []
    for sl, name, addr in rows:
        out.append({
            "source_agency": "National Housing Bank (NHB)",
            "source_list": "Companies Not Valid for Acceptance of Public Deposits",
            "case_unit": sl,
            "name": name,
            "father_name": "",
            "date_of_birth": "",
            "gender": "",
            "address": addr,
            "reward_amount": "",
            "details": "Certificate of Registration cancelled by NHB",
            "has_document": "No",
            "document_url": "",
            "detail_page_url": SOURCE_URL,
            "interpol_notice_id": "",
            "link_kind": "url_discovery",
            "scraped_at": scraped_at,
            "enrichment_status": "none",
        })
    return out


def save_to_csv(rows, path):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=CSV_FIELDS)
        w.writeheader()
        for r in rows:
            w.writerow({k: r.get(k, "") for k in CSV_FIELDS})
    print(f"Saved {len(rows)} records to {path}")


def run():
    print("=" * 60)
    print("NHB Companies Not Valid for Acceptance of Public Deposits (#90)")
    print("=" * 60)
    rows = scrape()
    save_to_csv(rows, OUTPUT_FILE)
    return rows


if __name__ == "__main__":
    run()
