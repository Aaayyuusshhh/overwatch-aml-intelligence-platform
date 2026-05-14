"""
Mumbai Police — Absconder List.

Source: https://mumbaipolice.gov.in/absconder_list

The page itself carries the canonical roster as a 50-row HTML table:
each row has a name (Marathi or English) and (for most rows) a link
to the individual absconder notice PDF in
/files/Absconder/<N>.pdf. The individual-notice PDFs are scanned
images, so we only extract the row text. Names appear in BOTH
Marathi and English — we keep only the ASCII-Latin English rows
to avoid downstream tokeniser issues, and emit one record per row.
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
LIST_URL = "https://mumbaipolice.gov.in/absconder_list"
OUTPUT_FILE = os.path.join(PROJECT_ROOT, "data",
                            "mumbai_police_absconders.csv")

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

# rows whose cell text is mostly Devanagari (Marathi) — drop
_DEVANAGARI = re.compile(r"[ऀ-ॿ]")


def _clean(s):
    if s is None:
        return ""
    return re.sub(r"\s+", " ", str(s).replace("\xa0", " ")).strip()


def _is_english(s):
    """Return True if the string is predominantly ASCII Latin (not
    mostly Devanagari)."""
    if not s:
        return False
    dev_count = len(_DEVANAGARI.findall(s))
    ascii_count = sum(1 for c in s if c.isascii() and (c.isalnum() or c.isspace()))
    return ascii_count > dev_count and ascii_count >= 3


def scrape():
    r = requests.get(LIST_URL, headers=UA, timeout=45, verify=False)
    if r.status_code != 200:
        raise RuntimeError(f"Mumbai Police: list page status {r.status_code}")
    soup = BeautifulSoup(r.text, "html.parser")
    table = soup.find("table")
    if table is None:
        raise RuntimeError("Mumbai Police: no <table> on page")
    scraped_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    out = []
    seen = set()
    for tr in table.find_all("tr"):
        cells = [_clean(td.get_text(" ", strip=True))
                 for td in tr.find_all(["td", "th"])]
        if len(cells) < 2:
            continue
        date = cells[0] if cells else ""
        name = cells[1] if len(cells) > 1 else ""
        if not name or not _is_english(name):
            continue
        # Get any PDF link on the row
        pdf = ""
        for a in tr.find_all("a", href=True):
            if a["href"].lower().endswith(".pdf"):
                pdf = urljoin(LIST_URL, a["href"])
                break
        key = name.lower()
        if key in seen:
            continue
        seen.add(key)
        detail_bits = []
        if date and not date.startswith("०००१"):
            detail_bits.append(f"Date: {date}")
        out.append({
            "source_agency": "Mumbai Police",
            "source_list":   "Absconder List",
            "case_unit":     "",
            "name":          name,
            "father_name":   "",
            "date_of_birth": "",
            "gender":        "",
            "address":       "",
            "reward_amount": "",
            "details":       " | ".join(detail_bits),
            "has_document":  "Yes" if pdf else "No",
            "document_url":  pdf,
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
    print("Mumbai Police Absconder List")
    print("=" * 60)
    rows = scrape()
    if not rows:
        raise RuntimeError("Mumbai Police: 0 rows parsed")
    save_to_csv(rows, OUTPUT_FILE)
    return rows


if __name__ == "__main__":
    run()
