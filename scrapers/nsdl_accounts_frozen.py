"""
NSDL Accounts Frozen (#191).

Source: https://nsdl.co.in/nsdlnews/accounts-frozen.php

The page is a single ~11 MB static HTML doc containing one giant
<table> with 25k+ rows of demat accounts that NSDL has frozen on
SEBI direction. Columns:

  Sr. No. | Client Name | Client PAN | Level of Freeze |
  Reference of SEBI directions | Order Date |
  Client Address (as provided in SEBI direction) |
  Whether PAN is debarred for opening of new account

All rows are present in the static HTML — no XHR needed. We map:
  name      = Client Name
  case_unit = Client PAN
  address   = Client Address (when not "-")
  details   = "Level: … | SEBI Ref: … | Order Date: … | PAN debarred: …"
"""

import csv
import os
import re
import time
from datetime import datetime

import requests
from bs4 import BeautifulSoup

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LIST_URL = "https://nsdl.co.in/nsdlnews/accounts-frozen.php"
OUTPUT_FILE = os.path.join(PROJECT_ROOT, "data",
                            "nsdl_accounts_frozen_191.csv")

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


def _clean(s):
    if s is None:
        return ""
    s = str(s).replace("\xa0", " ")
    s = re.sub(r"\s+", " ", s).strip()
    return s


def _fetch(retries=6):
    """NSDL serves the 11 MB page over a fragile connection; partial
    reads / chunked-encoding errors are common. Stream + retry."""
    last_err = None
    for i in range(retries):
        try:
            with requests.get(LIST_URL, headers=UA, timeout=120,
                              verify=False, stream=True) as r:
                if r.status_code != 200:
                    last_err = f"status={r.status_code}"
                    continue
                chunks = []
                for chunk in r.iter_content(chunk_size=65536):
                    if chunk:
                        chunks.append(chunk)
            body = b"".join(chunks)
            if len(body) > 1_000_000:
                return body.decode("utf-8", "ignore")
            last_err = f"too-short len={len(body)}"
        except Exception as e:
            last_err = f"{type(e).__name__}: {str(e)[:120]}"
        print(f"  fetch attempt {i+1} failed: {last_err} — retrying")
        time.sleep(5)
    raise RuntimeError(f"NSDL: fetch failed after {retries} retries: {last_err}")


def scrape():
    html = _fetch()
    soup = BeautifulSoup(html, "html.parser")
    table = soup.find("table")
    if table is None:
        raise RuntimeError("NSDL: no <table> on page")

    scraped_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    out = []
    # Walk rows, expecting 8 cells. Skip header row.
    for tr in table.find_all("tr"):
        cells = [_clean(td.get_text(" ", strip=True))
                 for td in tr.find_all(["td", "th"])]
        if len(cells) < 8:
            continue
        sr, name, pan, level, ref, order_date, addr, debarred = cells[:8]
        if sr.lower() in {"sr. no.", "sr.no.", "s.no."}:
            continue
        if not name or name.lower() in {"client name"}:
            continue
        # Skip placeholders ("-", "—") and single-character names.
        if name in {"-", "—"} or len(name) < 2:
            continue
        addr_clean = "" if addr in ("-", "—") else addr
        pan_clean = "" if pan in ("-", "—") else pan
        detail_bits = []
        if level and level != "-":
            detail_bits.append(f"Level: {level}")
        if ref and ref != "-":
            detail_bits.append(f"SEBI Ref: {ref}")
        if order_date and order_date != "-":
            detail_bits.append(f"Order Date: {order_date}")
        if debarred and debarred != "-":
            detail_bits.append(f"PAN debarred for new account: {debarred}")
        out.append({
            "source_agency": "National Securities Depository Limited (NSDL)",
            "source_list":   "Accounts Frozen on SEBI Direction",
            "case_unit":     pan_clean,
            "name":          name,
            "father_name":   "",
            "date_of_birth": "",
            "gender":        "",
            "address":       addr_clean,
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
    print("NSDL Accounts Frozen (#191)")
    print("=" * 60)
    rows = scrape()
    if not rows:
        raise RuntimeError("NSDL: 0 rows parsed")
    save_to_csv(rows, OUTPUT_FILE)
    return rows


if __name__ == "__main__":
    run()
