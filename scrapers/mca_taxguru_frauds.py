#!/usr/bin/env python3
"""TaxGuru — List of Companies Involved in Corporate Frauds / Chit Fund Scams.
URL: https://taxguru.in/corporate-law/list-companies-involved-corporate-fraudschit-fund-scams.html

Article reports 145 companies investigated by MCA for fraud / illegal deposit
taking. Data lives across 4 HTML tables: columns are Sl.NO., Name of Company,
State/UT, Suspected Quantum. Many cells lump multiple company names together
('A Ltd. B Ltd.'); we split on the period-space-uppercase pattern."""
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

SID = "mca_corporate_fraud_chit_fund"
AG = "Ministry of Corporate Affairs (MCA)"
LST = "Companies Involved in Corporate Frauds / Chit Fund Scams"
URL = ("https://taxguru.in/corporate-law/"
       "list-companies-involved-corporate-fraudschit-fund-scams.html")
H = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
     "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
     "Accept-Language": "en;q=0.9"}

FIELDS = ["source_agency", "source_list", "case_unit", "name", "father_name",
          "date_of_birth", "gender", "address", "reward_amount", "details",
          "has_document", "document_url", "detail_page_url",
          "interpol_notice_id", "link_kind", "scraped_at", "enrichment_status"]


def split_companies(cell: str) -> list[str]:
    """Split a cell that may contain multiple company names.
    Common separators: '. ' before Capital, or ' Ltd.' / 'Pvt.' followed by another capitalized phrase."""
    if not cell:
        return []
    # Normalize whitespace
    s = re.sub(r"\s+", " ", cell).strip()
    # Insert a sentinel after common company-ending tokens before splitting
    s = re.sub(r"\b(Ltd\.?|Limited|Pvt\.? Ltd\.?|Private Limited|Inc\.?|LLP|Corporation)\s+(?=[A-Z])",
               r"\1|||", s, flags=re.IGNORECASE)
    parts = [p.strip().strip(".") for p in s.split("|||") if p.strip()]
    # Filter very short / non-company-looking strings
    return [p for p in parts if len(p) >= 5 and re.search(r"[A-Za-z]", p)]


def run():
    now = datetime.now(timezone.utc).isoformat()
    out_path = os.path.join(DATA_DIR, f"{SID}.csv")
    print(f"[{SID}] {URL}")
    r = requests.get(URL, headers=H, timeout=30, verify=False)
    if r.status_code != 200:
        print(f"  status={r.status_code} — abort")
        return 0
    soup = BeautifulSoup(r.text, "html.parser")
    rows_out = []
    seen = set()
    tables = soup.find_all("table")
    for t in tables:
        trs = t.find_all("tr")
        for tr in trs:
            cells = [c.get_text(" ", strip=True) for c in tr.find_all(["td", "th"])]
            # We expect 4-column rows: sno, name, state, quantum
            if len(cells) < 2:
                continue
            sno = cells[0].strip().rstrip(".")
            # skip header-ish rows
            if sno.lower() in {"sl. no.", "sl.no.", "s.no.", "sno", "no.", "m", "t", "w", "f", "s"}:
                continue
            if not re.match(r"^\d+\.?$", sno):
                continue
            name_field = cells[1] if len(cells) >= 2 else ""
            state = cells[2] if len(cells) >= 3 else ""
            quantum = cells[3] if len(cells) >= 4 else ""
            for company in split_companies(name_field):
                k = company.lower()
                if k in seen:
                    continue
                seen.add(k)
                rows_out.append({
                    "source_agency": AG, "source_list": LST, "case_unit": sno,
                    "name": company[:200], "father_name": "", "date_of_birth": "",
                    "gender": "", "address": state, "reward_amount": quantum,
                    "details": (f"Investigated by MCA for corporate fraud / illegal "
                                f"deposit-taking | Sl.No.: {sno} | State: {state}"
                                + (f" | Suspected Quantum: {quantum}" if quantum else "")),
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
    for r_ in rows_out[:5]:
        print(f"    {r_['case_unit']}: {r_['name'][:50]:50s} | {r_['address']}")
    return len(rows_out)


if __name__ == "__main__":
    run()
