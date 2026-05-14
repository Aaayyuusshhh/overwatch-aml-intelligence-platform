"""
NHIDCL Blacklist / Debarment.

Source: https://nhidcl.com/en/black-list-debarment

The page renders three HTML tables (one per category of debarment):
  • Debarment of Contractors / Firms
  • Debarment of Consultants
  • Debarment of Officials

Each table has 5 columns:
  Sr. No. | Name | With Effect From | Duration | Document

The Document cell contains the file size; the actual PDF link is in
an <a href> inside the same cell.
"""

import csv
import os
import re
from datetime import datetime
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup
import warnings
warnings.filterwarnings("ignore")

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LIST_URL = "https://nhidcl.com/en/black-list-debarment"
OUTPUT_FILE = os.path.join(PROJECT_ROOT, "data",
                            "nhidcl_blacklist_debarment.csv")

CSV_FIELDS = [
    "source_agency", "source_list", "case_unit",
    "name", "father_name", "date_of_birth", "gender",
    "address", "reward_amount", "details",
    "has_document", "document_url", "detail_page_url",
    "interpol_notice_id", "link_kind", "scraped_at",
    "enrichment_status",
]

UA = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                    "Chrome/120.0.0.0 Safari/537.36"}


def _clean(s):
    if s is None:
        return ""
    return re.sub(r"\s+", " ", str(s).replace("\xa0", " ")).strip(" .,;-")


def scrape():
    r = requests.get(LIST_URL, headers=UA, timeout=45, verify=False)
    if r.status_code != 200:
        raise RuntimeError(f"NHIDCL: status {r.status_code}")
    soup = BeautifulSoup(r.text, "html.parser")
    scraped_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    out = []
    seen = set()
    # Walk every table; valid data rows have an integer Sr.No.
    for t in soup.find_all("table"):
        for tr in t.find_all("tr"):
            cells = tr.find_all(["td", "th"])
            if len(cells) < 5:
                continue
            sr  = _clean(cells[0].get_text(" ", strip=True))
            nm  = _clean(cells[1].get_text(" ", strip=True))
            eff = _clean(cells[2].get_text(" ", strip=True))
            dur = _clean(cells[3].get_text(" ", strip=True))
            doc_size = _clean(cells[4].get_text(" ", strip=True))
            doc_link = cells[4].find("a", href=True)
            doc_url  = urljoin(LIST_URL, doc_link["href"]) if doc_link else ""
            if not re.match(r"^\d+$", sr):
                continue
            if not nm:
                continue
            key = (nm.lower(), eff)
            if key in seen:
                continue
            seen.add(key)
            out.append({
                "source_agency": "National Highways & Infrastructure Development Corporation Limited (NHIDCL)",
                "source_list":   "Blacklist / Debarment",
                "case_unit":     "",
                "name":          nm,
                "father_name":   "",
                "date_of_birth": "",
                "gender":        "",
                "address":       "",
                "reward_amount": "",
                "details":       (f"With Effect From: {eff} | Duration: {dur}"
                                  + (f" | Document size: {doc_size}" if doc_size else "")),
                "has_document":  "Yes" if doc_url else "No",
                "document_url":  doc_url,
                "detail_page_url": LIST_URL,
                "interpol_notice_id": "",
                "link_kind":     "recon_discovery",
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
    print("NHIDCL Blacklist / Debarment")
    print("=" * 60)
    rows = scrape()
    if not rows:
        raise RuntimeError("NHIDCL: 0 rows parsed")
    save_to_csv(rows, OUTPUT_FILE)
    return rows


if __name__ == "__main__":
    run()
