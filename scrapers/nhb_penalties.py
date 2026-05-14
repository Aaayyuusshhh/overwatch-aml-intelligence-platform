"""
NHB Penalties.

Source: https://nhb.org.in/penalties

A single HTML page with one wide table of every monetary penalty
imposed by the National Housing Bank on a housing finance company.
Schema:

  Sl. No. | Company's Name | Date | Reasons for imposition of Penalty | Amount of penalty (₹)

Rows are grouped by financial year: a separator row has only the
"YYYY-YY" string in the Company column with everything else blank,
and is followed by per-year rows with Sl. No. restarting at 1.

We emit one record per data row, mapping:
  name        = Company's Name
  reward_amount = Amount of penalty (used as the monetary field;
                  pipeline's "reward_amount" doubles as monetary value)
  details     = Date: … | Year: … | Reason: …
"""

import csv
import os
import re
from datetime import datetime

import requests
from bs4 import BeautifulSoup
import warnings
warnings.filterwarnings("ignore")

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LIST_URL = "https://nhb.org.in/penalties"
OUTPUT_FILE = os.path.join(PROJECT_ROOT, "data", "nhb_penalties_95.csv")

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
      "Accept": "text/html,*/*;q=0.8"}

FY_RE = re.compile(r"^\d{4}-\d{2}$")


def _clean(s):
    if s is None:
        return ""
    return re.sub(r"\s+", " ", str(s).replace("\xa0", " ")).strip()


def scrape():
    r = requests.get(LIST_URL, headers=UA, timeout=45, verify=False)
    if r.status_code != 200:
        raise RuntimeError(f"NHB Penalties: status {r.status_code}")
    soup = BeautifulSoup(r.text, "html.parser")
    table = soup.find("table")
    if table is None:
        raise RuntimeError("NHB Penalties: no <table> on page")
    rows = table.find_all("tr")
    scraped_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    fy = ""
    out = []
    for tr in rows:
        cells = [_clean(td.get_text(" ", strip=True))
                 for td in tr.find_all(["td", "th"])]
        if len(cells) < 5:
            continue
        sl, company, date, reason, amount = cells[:5]
        # Skip header
        if sl.lower().startswith("sl. no") or sl.lower() == "sl.no.":
            continue
        # Year-separator row: company is "YYYY-YY", others blank
        if FY_RE.match(company) and not (date or reason or amount):
            fy = company
            continue
        if not company:
            continue
        detail_bits = []
        if date:   detail_bits.append(f"Date: {date}")
        if fy:     detail_bits.append(f"Year: {fy}")
        if reason: detail_bits.append(f"Reason: {reason}")
        out.append({
            "source_agency": "National Housing Bank (NHB)",
            "source_list":   "Penalties",
            "case_unit":     "",
            "name":          company,
            "father_name":   "",
            "date_of_birth": "",
            "gender":        "",
            "address":       "",
            "reward_amount": amount,
            "details":       " | ".join(detail_bits),
            "has_document":  "No",
            "document_url":  "",
            "detail_page_url": LIST_URL,
            "interpol_notice_id": "",
            "link_kind":     "manual_discovery",
            "scraped_at":    scraped_at,
            "enrichment_status": "",
        })
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
    print("NHB Penalty Orders")
    print("=" * 60)
    rows = scrape()
    if not rows:
        raise RuntimeError("NHB Penalties: 0 rows parsed")
    save_to_csv(rows, OUTPUT_FILE)
    return rows


if __name__ == "__main__":
    run()
