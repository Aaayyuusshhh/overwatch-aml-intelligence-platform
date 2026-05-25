#!/usr/bin/env python3
"""Vanishing Companies — published by MCA, mirrored at watchoutinvestors.com.

The MCA vanishing-companies list is no longer hosted directly on mca.gov.in;
the canonical mirror is watchoutinvestors.com/dcavanish.asp. This scraper
pulls the main listing (20 companies + their directors). Each row's cells:
  [0] sno, [1] company, [3] entity (dup of [1]), [4] person names

Persons in cell[4] are concatenated by spaces. We split on ALL-CAPS-word
boundaries to recover individual director names.
"""
from __future__ import annotations
import csv, os, re, warnings, urllib3
from datetime import datetime, timezone
import requests
from bs4 import BeautifulSoup

warnings.filterwarnings("ignore")
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.path.join(_PROJECT_ROOT, "data")
os.makedirs(DATA_DIR, exist_ok=True)

SID = "mca_vanishing_companies"
AG = "Ministry of Corporate Affairs (MCA)"
LST = "Vanishing Companies (MCA via WatchOut)"
URL = "https://www.watchoutinvestors.com/dcavanish.asp?id=1181227"
INDEX_URL = "https://www.watchoutinvestors.com/default2.asp?page=data_dcavanish.htm"
H = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"}

FIELDS = ["source_agency", "source_list", "case_unit", "name", "father_name",
          "date_of_birth", "gender", "address", "reward_amount", "details",
          "has_document", "document_url", "detail_page_url",
          "interpol_notice_id", "link_kind", "scraped_at", "enrichment_status"]


def split_persons(s: str) -> list[str]:
    """Split concatenated ALL-CAPS person names. e.g. 'DHAVAL A.JHAVERI JAYESH R.MOR' -> ['DHAVAL A.JHAVERI', 'JAYESH R.MOR']."""
    if not s:
        return []
    s = re.sub(r"\s+", " ", s).strip()
    # Insert sentinel before any token that looks like a NEW person:
    #   <SPACE><Capital word of >=4 letters> AND the prev word doesn't end in '.'
    # Heuristic: tokens of pattern UPPERCASE [+ initial.] are name parts;
    # a new person usually starts with a >=4-letter all-caps word.
    parts = []
    cur = []
    tokens = s.split(" ")
    for tok in tokens:
        if not tok:
            continue
        # Starts new person if it's a 3+ letter all-caps word (likely first name)
        # AND the current buffer already has 1+ tokens
        if (cur and re.fullmatch(r"[A-Z][A-Z]{2,}", tok)):
            parts.append(" ".join(cur))
            cur = [tok]
        else:
            cur.append(tok)
    if cur:
        parts.append(" ".join(cur))
    return [p.strip() for p in parts if len(p.strip()) > 3]


def run():
    now = datetime.now(timezone.utc).isoformat()
    out_path = os.path.join(DATA_DIR, f"{SID}.csv")
    print(f"[{SID}] {URL}")
    r = requests.get(URL, headers=H, timeout=90, verify=False)
    if r.status_code != 200:
        print(f"  status={r.status_code} — abort")
        return 0
    soup = BeautifulSoup(r.text, "html.parser")
    tables = soup.find_all("table")
    if not tables:
        print("  no table found")
        return 0
    rows_out = []
    seen = set()
    for tr in tables[0].find_all("tr"):
        cells = [c.get_text(" ", strip=True) for c in tr.find_all("td")]
        if not cells or not cells[0].strip().isdigit():
            continue
        sno = cells[0].strip()
        company = cells[1].strip() if len(cells) > 1 else ""
        persons = cells[4].strip() if len(cells) > 4 else ""
        charges_idx = None
        # find charges + actions cells (variable position)
        charges = next((c for c in cells[5:] if c and len(c) > 30), "")
        if company and len(company) > 3:
            k = ("co", company.lower())
            if k not in seen:
                seen.add(k)
                rows_out.append({
                    "source_agency": AG, "source_list": LST, "case_unit": sno,
                    "name": company[:200], "father_name": "",
                    "date_of_birth": "", "gender": "", "address": "",
                    "reward_amount": "",
                    "details": (f"Vanishing Company (MCA list) | Sl.No.: {sno}"
                                + (f" | Charges: {charges[:200]}" if charges else "")),
                    "has_document": "No", "document_url": "",
                    "detail_page_url": URL, "interpol_notice_id": "",
                    "link_kind": "html", "scraped_at": now,
                    "enrichment_status": "",
                })
        for person in split_persons(persons):
            k = ("p", person.lower())
            if k in seen:
                continue
            seen.add(k)
            rows_out.append({
                "source_agency": AG, "source_list": LST, "case_unit": sno,
                "name": person[:200], "father_name": "",
                "date_of_birth": "", "gender": "", "address": "",
                "reward_amount": "",
                "details": (f"Director / officer of vanishing company '{company[:80]}' "
                            f"(MCA list) | Sl.No.: {sno}"),
                "has_document": "No", "document_url": "",
                "detail_page_url": URL, "interpol_notice_id": "",
                "link_kind": "html", "scraped_at": now,
                "enrichment_status": "",
            })
    with open(out_path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows_out)
    print(f"  {SID}: {len(rows_out)} rows -> {out_path}")
    for r_ in rows_out[:6]:
        print(f"    [{r_['case_unit']}] {r_['name'][:60]:60s} ({r_['details'][:50]})")
    return len(rows_out)


if __name__ == "__main__":
    run()
