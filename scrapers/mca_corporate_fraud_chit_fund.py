#!/usr/bin/env python3
"""TaxGuru article scraper — 145 companies allegedly involved in corporate frauds
and chit fund scams under MCA investigation.

The MCA's original PIB release is no longer indexed; TaxGuru hosts the full
Annexure I as 4 HTML tables (16+63+51+16 = 146 rows by serial number; rows
often list multiple co-defendant companies in one cell).
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

FIELDS = ["source_agency", "source_list", "case_unit", "name", "father_name",
          "date_of_birth", "gender", "address", "reward_amount", "details",
          "has_document", "document_url", "detail_page_url",
          "interpol_notice_id", "link_kind", "scraped_at", "enrichment_status"]

SOURCE_ID = "mca_corporate_fraud_chit_fund"
LIST_NAME = "Companies Involved in Corporate Frauds / Chit Fund Scams"
AGENCY = "Ministry of Corporate Affairs (MCA)"
PAGE_URL = ("https://taxguru.in/corporate-law/"
            "list-companies-involved-corporate-fraudschit-fund-scams.html")
H = {
    "User-Agent": ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-IN,en-US;q=0.9,en;q=0.8",
}


def split_company_cell(cell_text):
    """Cells may contain one or more company names concatenated
    (e.g. "Basil International Ltd.Vamshi Chemicals Ltd.Nixil Industries Ltd.").
    Split on word-boundary suffixes like "Ltd.", "Limited.", "Pvt. Ltd.",
    "Private Limited." — each must be at a word boundary so we don't split
    "Cosmetics" on "Co".
    """
    if not cell_text:
        return []
    txt = re.sub(r"\s+", " ", cell_text.strip())
    # Word-bounded suffix patterns. \b prevents matching "Co" inside "Cosmetics".
    # Order matters: longer alternatives first to avoid partial matches.
    suffix_re = re.compile(
        r"(?:"
        r"Pvt\.?\s*Ltd\.?|"
        r"\bPrivate\s+Limited|"
        r"\bLimited|"
        r"\bLtd\.?|"
        r"\bLLP"
        r")\.?",
        flags=re.IGNORECASE)
    matches = list(suffix_re.finditer(txt))
    if not matches:
        return [txt] if txt else []
    out = []
    start = 0
    for m in matches:
        end = m.end()
        piece = txt[start:end].strip(" .,-")
        # Trim a stray leading "." that came from a previous cut
        piece = re.sub(r"^[\s.,-]+", "", piece)
        if piece and len(piece) > 3:
            out.append(piece)
        start = end
    return out


def main():
    print(f"=== {SOURCE_ID} ===")
    r = requests.get(PAGE_URL, headers=H, timeout=30, verify=False)
    if r.status_code != 200:
        print(f"  HTTP {r.status_code}")
        return 0
    soup = BeautifulSoup(r.text, "html.parser")
    tables = soup.find_all("table")
    # The article has 4 data tables (split because TaxGuru's editor inserts
    # page breaks). Only the FIRST has a header row. Tables 2-4 are
    # continuations. Identify them by row count and exclude the wp-calendar
    # table by CSS class.
    data_tables = []
    for t in tables:
        cls = t.get("class") or []
        if any("calendar" in c.lower() for c in cls):
            continue
        rows = t.find_all("tr")
        if len(rows) >= 10:  # all 4 data tables have >= 16 rows
            data_tables.append(t)
    print(f"  found {len(data_tables)} data tables")

    now = datetime.now(timezone.utc).isoformat()
    out_path = os.path.join(DATA_DIR, f"{SOURCE_ID}.csv")
    total = 0
    seen = set()
    with open(out_path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS, extrasaction="ignore")
        w.writeheader()
        cur_sno = ""
        cur_state = ""
        cur_amount = ""
        for ti, t in enumerate(data_tables):
            rows = t.find_all("tr")
            for ri, tr in enumerate(rows):
                cells = [c.get_text(strip=True) for c in tr.find_all(["td", "th"])]
                if len(cells) < 2:
                    continue
                # Skip header row
                if ri == 0 and any("name of the company" in c.lower() for c in cells):
                    continue
                # Detect format. Common: [sno, name, state, amount]. Sometimes
                # only [sno] or [sno, name] (continuation/wrap rows).
                if len(cells) >= 4:
                    sno, name_cell, state_cell, amount_cell = cells[0], cells[1], cells[2], cells[3]
                elif len(cells) == 3:
                    sno, name_cell, state_cell = cells[0], cells[1], cells[2]
                    amount_cell = ""
                elif len(cells) == 2:
                    sno, name_cell = cells[0], cells[1]
                    state_cell, amount_cell = "", ""
                else:
                    sno = cells[0]
                    name_cell, state_cell, amount_cell = "", "", ""
                # Carry forward if cell is missing (often empty cells inherit)
                if sno and sno.strip(" .)"):
                    cur_sno = sno.strip(" .)")
                if state_cell:
                    cur_state = state_cell
                if amount_cell:
                    cur_amount = amount_cell
                if not name_cell:
                    continue
                companies = split_company_cell(name_cell)
                for co in companies:
                    key = co.lower()
                    if key in seen:
                        continue
                    seen.add(key)
                    details = (f"Sl.No.: {cur_sno}"
                               f" | State/UT: {cur_state}"
                               f" | Suspected Quantum: {cur_amount}"
                               f" | Source: MCA investigation list (Annexure I)"
                               f" | Mirror: TaxGuru article")
                    w.writerow({
                        "source_agency": AGENCY, "source_list": LIST_NAME,
                        "case_unit": cur_state, "name": co[:200],
                        "father_name": "", "date_of_birth": "",
                        "gender": "", "address": "",
                        "reward_amount": cur_amount[:60],
                        "details": details, "has_document": "No",
                        "document_url": "", "detail_page_url": PAGE_URL,
                        "interpol_notice_id": "", "link_kind": "html",
                        "scraped_at": now, "enrichment_status": "",
                    })
                    total += 1
    print(f"  DONE: {SOURCE_ID} -> {out_path}  rows={total}")
    return total


if __name__ == "__main__":
    main()
