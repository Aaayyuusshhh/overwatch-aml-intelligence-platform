"""
AMFI Suspended ARN Holders (#234).

Source: https://www.amfiindia.com/locate-distributor/suspended-arn

The page contains a single HTML table with four columns:
  Sr. No. | ARN | Name of ARN Holder | With Effect From

Each row is a mutual-fund distributor whose ARN registration has been
suspended (or terminated). At time of scrape there are ~40 records.
"""

import csv
import os
import re
from datetime import datetime

import requests
from bs4 import BeautifulSoup

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LIST_URL = "https://www.amfiindia.com/locate-distributor/suspended-arn"
OUTPUT_FILE = os.path.join(PROJECT_ROOT, "data",
                            "amfi_suspended_arn_234.csv")

CSV_FIELDS = [
    "source_agency", "source_list", "case_unit",
    "name", "father_name", "date_of_birth", "gender",
    "address", "reward_amount", "details",
    "has_document", "document_url", "detail_page_url",
    "interpol_notice_id", "link_kind", "scraped_at",
    "enrichment_status",
]

UA = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                    "Chrome/120.0.0.0 Safari/537.36",
      "Accept": "text/html"}


def _clean(s):
    if s is None:
        return ""
    return re.sub(r"\s+", " ", str(s).replace("\xa0", " ")).strip()


def scrape():
    r = requests.get(LIST_URL, headers=UA, timeout=30, verify=False)
    if r.status_code != 200:
        raise RuntimeError(f"AMFI: status {r.status_code}")
    soup = BeautifulSoup(r.text, "html.parser")
    rows = []
    scraped_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    # Pick the first table whose first non-blank row looks like the
    # suspended-ARN header.
    for t in soup.find_all("table"):
        trs = t.find_all("tr")
        if len(trs) < 2:
            continue
        for tr in trs:
            cells = [_clean(c.get_text(" ", strip=True))
                     for c in tr.find_all(["td", "th"])]
            if len(cells) < 4:
                continue
            sr, arn, name, eff = cells[:4]
            # Skip header
            if sr.lower() in {"sr. no.", "sr.no.", "s.no."}:
                continue
            if name.lower() in {"name of arn holder", "arn holder", "name"}:
                continue
            if not arn or not name:
                continue
            rows.append({
                "source_agency": "Association of Mutual Funds in India (AMFI)",
                "source_list":   "Suspended ARN Holders",
                "case_unit":     arn,
                "name":          name,
                "father_name":   "",
                "date_of_birth": "",
                "gender":        "",
                "address":       "",
                "reward_amount": "",
                "details":       f"ARN: {arn} | Suspended from: {eff}",
                "has_document":  "No",
                "document_url":  "",
                "detail_page_url": LIST_URL,
                "interpol_notice_id": "",
                "link_kind":     "manual_discovery",
                "scraped_at":    scraped_at,
                "enrichment_status": "",
            })
    return rows


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
    print("AMFI Suspended ARN Holders (#234)")
    print("=" * 60)
    rows = scrape()
    if not rows:
        raise RuntimeError("AMFI: 0 rows")
    save_to_csv(rows, OUTPUT_FILE)
    return rows


if __name__ == "__main__":
    run()
