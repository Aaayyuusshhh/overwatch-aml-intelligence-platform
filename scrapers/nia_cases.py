"""
NIA Cases (#103).

Source: https://nia.gov.in/nia-cases (Drupal-views listing)

The page has a single 5-column table:
  Sr. No. | Year | Case No. | Case Title | Organisation / Gang

Pagination via ?page=0..4 (page 5+ returns empty 0-row tables).
26 rows per page × 5 pages ≈ 130 cases.

The Case Title is prose describing the offence with accused names
embedded ("A bomb explosion at Sundrpada area, in which Lijatun
Bibi…"); we keep the case_no as case_unit and use the title as the
canonical name so screening on either the case_no, the gang name,
or any embedded person/place name works.
"""

import csv
import os
import re
import time
from datetime import datetime

import requests
from bs4 import BeautifulSoup
import warnings
warnings.filterwarnings("ignore")

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LIST_URL = "https://nia.gov.in/nia-cases"
OUTPUT_FILE = os.path.join(PROJECT_ROOT, "data", "nia_cases_103.csv")

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
    return re.sub(r"\s+", " ", str(s).replace("\xa0", " ")).strip(" .,;-")


def scrape():
    sess = requests.Session()
    sess.headers.update(UA)
    sess.verify = False
    scraped_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    out = []
    seen = set()
    for page in range(0, 12):
        url = f"{LIST_URL}?page={page}" if page else LIST_URL
        r = sess.get(url, timeout=45)
        if r.status_code != 200:
            print(f"  page {page}: status {r.status_code}")
            break
        soup = BeautifulSoup(r.text, "html.parser")
        t = soup.find("table")
        if not t:
            break
        trs = t.find_all("tr")
        added = 0
        for tr in trs:
            cells = tr.find_all(["td", "th"])
            if len(cells) < 5:
                continue
            sr, year, case_no, title, gang = (
                _clean(c.get_text(" ", strip=True)) for c in cells[:5]
            )
            if not re.match(r"^\d+$", sr):
                continue                    # header / pagination row
            if not case_no:
                continue
            key = case_no.lower()
            if key in seen:
                continue
            seen.add(key)
            # Name: prefer the Organisation/Gang when present (it's an
            # entity); otherwise fall back to the full Case Title.
            name = gang if gang else title
            detail_bits = [f"Case No: {case_no}", f"Year: {year}",
                            f"Title: {title}"]
            if gang:
                detail_bits.append(f"Gang/Organisation: {gang}")
            out.append({
                "source_agency": "National Investigation Agency (NIA)",
                "source_list":   "Cases",
                "case_unit":     case_no,
                "name":          name,
                "father_name":   "",
                "date_of_birth": "",
                "gender":        "",
                "address":       "",
                "reward_amount": "",
                "details":       " | ".join(detail_bits),
                "has_document":  "No",
                "document_url":  "",
                "detail_page_url": LIST_URL,
                "interpol_notice_id": "",
                "link_kind":     "manual_discovery",
                "scraped_at":    scraped_at,
                "enrichment_status": "",
            })
            added += 1
        print(f"  page {page}: +{added}  total={len(out)}")
        if added == 0 and page > 0:
            break
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
    print("NIA Cases (#103)")
    print("=" * 60)
    rows = scrape()
    if not rows:
        raise RuntimeError("NIA Cases: 0 rows parsed")
    save_to_csv(rows, OUTPUT_FILE)
    return rows


if __name__ == "__main__":
    run()
