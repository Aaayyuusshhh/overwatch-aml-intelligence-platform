"""
Kerala Police Wanted Persons (#215).

Source: https://keralapolice.gov.in/page/wanted-persons

The page is a regular static page; the wanted persons live in a single
HTML table with two cells per row: (photo, descriptive text). The
descriptive text follows the format

  "<NAME> CRIME NO:<n>/<yy> U/s.<sections> OF <POLICE_STATION> @<alias?>
   <father> S/o ... <address>"

We capture the first sentence-ish chunk as the name (everything before
"CRIME NO") and stash the full descriptor in details. At the time of
this scrape the page lists a single wanted person; the scraper is
written to handle however many appear.
"""

import csv
import os
import re
from datetime import datetime
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LIST_URL = "https://keralapolice.gov.in/page/wanted-persons"
OUTPUT_FILE = os.path.join(PROJECT_ROOT, "data", "kerala_police_wanted_215.csv")

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

CRIME_RE = re.compile(r"\bCRIME\s*NO[:\s]", re.I)


def _clean(s):
    if s is None:
        return ""
    return re.sub(r"\s+", " ", str(s).replace("\xa0", " ")).strip()


def _parse_row(text, photo_url):
    """Parse one descriptive row text into a record dict, or None."""
    t = _clean(text)
    if not t or len(t) < 5:
        return None
    m = CRIME_RE.search(t)
    if m:
        name_part = _clean(t[: m.start()]).rstrip(".,;-")
        rest = _clean(t[m.start():])
    else:
        # No "CRIME NO" anchor — keep the first up-to-6-word chunk as
        # the name and the rest as details.
        words = t.split()
        name_part = " ".join(words[:6])
        rest = " ".join(words[6:])
    if not name_part:
        return None
    return name_part, rest, photo_url


def scrape():
    r = requests.get(LIST_URL, headers=UA, timeout=30, verify=False)
    if r.status_code != 200:
        raise RuntimeError(f"Kerala Police: status {r.status_code}")
    soup = BeautifulSoup(r.text, "html.parser")
    tables = soup.find_all("table")
    if not tables:
        raise RuntimeError("Kerala Police: no <table> on page")
    scraped_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    out = []
    seen = set()
    for t in tables:
        for tr in t.find_all("tr"):
            cells = tr.find_all(["td", "th"])
            if len(cells) < 2:
                continue
            img = cells[0].find("img")
            photo = urljoin(LIST_URL, img.get("src", "")) if img else ""
            text = cells[1].get_text(" ", strip=True)
            parsed = _parse_row(text, photo)
            if not parsed:
                continue
            name, rest, photo = parsed
            key = (name, rest[:80])
            if key in seen:
                continue
            seen.add(key)
            out.append({
                "source_agency": "Kerala Police",
                "source_list": "Wanted Persons",
                "case_unit": "",
                "name": name,
                "father_name": "",
                "date_of_birth": "",
                "gender": "",
                "address": "",
                "reward_amount": "",
                "details": rest,
                "has_document": "Yes" if photo else "No",
                "document_url": photo,
                "detail_page_url": LIST_URL,
                "interpol_notice_id": "",
                "link_kind": "homepage_scan",
                "scraped_at": scraped_at,
                "enrichment_status": "",
            })
    print(f"  parsed {len(out)} wanted person rows")
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
    print("Kerala Police Wanted Persons (#215)")
    print("=" * 60)
    rows = scrape()
    if not rows:
        raise RuntimeError("Kerala Police: 0 rows parsed")
    save_to_csv(rows, OUTPUT_FILE)
    return rows


if __name__ == "__main__":
    run()
