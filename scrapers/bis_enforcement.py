"""
BIS Enforcement Activities (#4).

Source: POST https://www.services.bis.gov.in/php/BIS_2.0/consumer/enforcement-activities/fetch
Form payload: type=<ss|compounding|judgements>&fy=<YYYY-YYYY>

The endpoint returns {"success": true, "html": "<table>...</table>"}
with one row per activity. Columns vary slightly by type but are
roughly: Sr No | Region | Branch | Date of S&S | Name of Firm |
Product Name | District | State.
"""

import csv
import os
import re
import time
from datetime import datetime

import requests
from bs4 import BeautifulSoup

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
API_URL = ("https://www.services.bis.gov.in/php/BIS_2.0/"
           "consumer/enforcement-activities/fetch")
OUTPUT_FILE = os.path.join(PROJECT_ROOT, "data", "bis_enforcement_4.csv")

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
      "X-Requested-With": "XMLHttpRequest",
      "Content-Type": "application/x-www-form-urlencoded",
      "Accept": "application/json"}

TYPE_LABELS = {
    "ss":          "Successful Enforcements",
    "compounding": "Compounding of Offence",
    "judgements":  "Judgements",
}
YEARS = ["2022-2023", "2023-2024", "2024-2025", "2025-2026", "2026-2027"]


def _clean(s):
    if s is None:
        return ""
    return re.sub(r"\s+", " ", str(s).replace("\xa0", " ")).strip()


def _parse_table(html, type_key, label, fy):
    """Return list of records from one HTML response. Header text is
    used as the field-name map so column-order shifts between types
    don't break extraction."""
    soup = BeautifulSoup(html, "html.parser")
    table = soup.find("table")
    if not table:
        return []
    rows = table.find_all("tr")
    if len(rows) < 2:
        return []
    headers = [_clean(c.get_text(" ", strip=True)).lower()
               for c in rows[0].find_all(["th", "td"])]
    # build header -> idx map
    idx = {}
    for i, h in enumerate(headers):
        for k in ("name of firm", "firm name", "name of accused",
                  "person name", "name", "region", "branch", "date",
                  "product", "district", "state", "section", "remarks",
                  "court", "case", "penalty"):
            if k in h and k not in idx:
                idx[k] = i
    out = []
    def _col(name_key, default=""):
        i = idx.get(name_key)
        if i is None or i >= len(cells):
            return default
        return cells[i]

    for tr in rows[1:]:
        cells = [_clean(c.get_text(" ", strip=True))
                 for c in tr.find_all(["td", "th"])]
        if not cells:
            continue
        name = (_col("name of firm") or _col("firm name")
                or _col("name of accused") or _col("name"))
        # Fall back to position 4 (typical BIS layout) only when the
        # header match missed AND there are enough cells.
        if not name and len(cells) > 4:
            name = cells[4]
        name = _clean(name)
        if not name or name.lower() in {"name of firm", "firm name"}:
            continue
        region   = _col("region")
        branch   = _col("branch")
        date     = _col("date")
        product  = _col("product")
        district = _col("district")
        state    = _col("state")
        address  = ", ".join(p for p in (district, state) if p)
        detail_parts = [
            f"Type: {label}",
            f"FY: {fy}",
            f"Region: {region}" if region else "",
            f"Branch: {branch}" if branch else "",
            f"Date: {date}" if date else "",
            f"Product: {product}" if product else "",
        ]
        out.append((name, address, " | ".join(p for p in detail_parts if p)))
    return out


def scrape():
    scraped_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    sess = requests.Session()
    sess.verify = False
    out = []
    for type_key, label in TYPE_LABELS.items():
        for fy in YEARS:
            try:
                r = sess.post(API_URL, headers=UA,
                               data={"type": type_key, "fy": fy}, timeout=30)
            except Exception as e:
                print(f"  ERR {type_key} {fy}: {type(e).__name__}: {e}")
                continue
            if r.status_code != 200:
                print(f"  {r.status_code} {type_key} {fy}")
                continue
            try:
                payload = r.json()
            except Exception:
                print(f"  non-JSON {type_key} {fy}")
                continue
            html = payload.get("html") or ""
            if not html:
                print(f"  empty html {type_key} {fy}")
                continue
            recs = _parse_table(html, type_key, label, fy)
            print(f"  {type_key:<12} {fy}: {len(recs)} records")
            for name, address, details in recs:
                out.append({
                    "source_agency": "Bureau of Indian Standards (BIS)",
                    "source_list":   "Enforcement Activities",
                    "case_unit":     "",
                    "name":          name,
                    "father_name":   "",
                    "date_of_birth": "",
                    "gender":        "",
                    "address":       address,
                    "reward_amount": "",
                    "details":       details,
                    "has_document":  "No",
                    "document_url":  "",
                    "detail_page_url": "https://www.bis.gov.in/enforcement/",
                    "interpol_notice_id": "",
                    "link_kind":     "api_discovery",
                    "scraped_at":    scraped_at,
                    "enrichment_status": "",
                })
            time.sleep(1.5)
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
    print("BIS Enforcement Activities (#4)")
    print("=" * 60)
    rows = scrape()
    save_to_csv(rows, OUTPUT_FILE)
    return rows


if __name__ == "__main__":
    run()
