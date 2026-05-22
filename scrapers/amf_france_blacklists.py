#!/usr/bin/env python3
"""AMF France — public blacklist of unauthorised companies and websites.

URL: https://www.amf-france.org/en/warnings/blacklists
The page is a paginated table — each row: company/website name, category
('AMF Usurpation', 'Forex', 'Binary options', 'Crypto-assets', ...), date.
There are also PDF lists per category linked below the table; we only
parse the live HTML table (rows are added on top as new entries land).
"""
from __future__ import annotations
import csv, os, sys, time, warnings, re
from datetime import datetime, timezone
from urllib.parse import urljoin, urlparse
import requests
from bs4 import BeautifulSoup

warnings.filterwarnings("ignore")
import urllib3
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.path.join(_PROJECT_ROOT, "data")
os.makedirs(DATA_DIR, exist_ok=True)

SID = "amf_france_blacklists"
AG = "Autorité des marchés financiers (AMF)"
LST = "Blacklists of unauthorised companies"
URL = "https://www.amf-france.org/en/warnings/blacklists"
H = {"User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/125.0 Safari/537.36"),
     "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
     "Accept-Language": "en;q=0.9"}

FIELDS = ["source_agency", "source_list", "case_unit", "name", "father_name",
          "date_of_birth", "gender", "address", "reward_amount", "details",
          "has_document", "document_url", "detail_page_url",
          "interpol_notice_id", "link_kind", "scraped_at", "enrichment_status"]


def clean(s: str) -> str:
    if not s:
        return ""
    s = re.sub(r"\s*New\s*$", "", s, flags=re.IGNORECASE)
    return s.strip()


def run():
    now = datetime.now(timezone.utc).isoformat()
    out = os.path.join(DATA_DIR, f"{SID}.csv")
    print(f"[{SID}] {URL}")

    r = requests.get(URL, headers=H, timeout=30, verify=False)
    if r.status_code != 200:
        print(f"  status={r.status_code} — abort")
        return 0
    soup = BeautifulSoup(r.text, "html.parser")

    rows_out = []
    tables = soup.find_all("table")
    for tbl in tables:
        for tr in tbl.find_all("tr"):
            cells = [c.get_text(" ", strip=True) for c in tr.find_all(["td", "th"])]
            if len(cells) < 3:
                continue
            if cells[0].lower().startswith("last name") or cells[0].lower() == "name":
                continue
            name = clean(cells[0])
            if not name or len(name) < 3:
                continue
            category = clean(cells[1])
            date_str = clean(cells[2]) if len(cells) >= 3 else ""
            # link to detail (if any)
            a = tr.find("a", href=True)
            link = urljoin(URL, a["href"]) if a else URL
            details = f"Category: {category}"
            if date_str:
                details += f" | Published: {date_str}"
            rows_out.append({
                "source_agency": AG, "source_list": LST, "case_unit": "",
                "name": name, "father_name": "", "date_of_birth": "",
                "gender": "", "address": "", "reward_amount": "",
                "details": details, "has_document": "Yes" if a else "No",
                "document_url": link if a else "", "detail_page_url": URL,
                "interpol_notice_id": "", "link_kind": "html",
                "scraped_at": now, "enrichment_status": "",
            })
    # Also pull PDF list URLs as "list-of-lists" reference entries
    for a in soup.find_all("a", href=True):
        href = a["href"]
        if not href.lower().endswith(".pdf"):
            continue
        if "blacklist" not in href.lower() and "listenoire" not in href.lower():
            continue
        pdf_url = urljoin(URL, href)
        pdf_text = a.get_text(" ", strip=True) or os.path.basename(urlparse(pdf_url).path)
        rows_out.append({
            "source_agency": AG, "source_list": LST, "case_unit": "",
            "name": pdf_text[:120], "father_name": "", "date_of_birth": "",
            "gender": "", "address": "", "reward_amount": "",
            "details": "AMF blacklist PDF reference",
            "has_document": "Yes", "document_url": pdf_url,
            "detail_page_url": URL, "interpol_notice_id": "",
            "link_kind": "pdf", "scraped_at": now, "enrichment_status": "",
        })

    # de-dup by (name, details) — page shows recent entries from multiple PDFs
    seen = set()
    uniq = []
    for r_ in rows_out:
        k = (r_["name"].lower(), r_["details"].lower())
        if k in seen:
            continue
        seen.add(k)
        uniq.append(r_)

    with open(out, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS, extrasaction="ignore")
        w.writeheader()
        w.writerows(uniq)
    print(f"  {SID}: {len(uniq)} rows -> {out}")
    for r_ in uniq[:3]:
        print(f"    {r_['name'][:50]:50s} | {r_['details'][:60]}")
    return len(uniq)


if __name__ == "__main__":
    run()
