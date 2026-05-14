"""
scrapers/bse_debarred_entities.py — BSE Debarred Entities + Action vs
Trading Members.

Three direct-download files, refreshed daily on bseindia.com:

  1. https://www.bseindia.com/download/DebarredEntities/SEBI%20DEBARRED%20<DATE>.zip
       -> contains a single .xlsx of SEBI-debarred entities.
       -> mapped to canonical source #109 (List of Debarred Entities
          based on Orders / Directions).

  2. https://www.bseindia.com/download/DebarredEntities/Other%20Competent%20Authorities%20DEBARRED%20<DATE>.zip
       -> .xlsx of entities debarred by Other Competent Authorities.
       -> mapped to canonical source #110.

  3. https://www.bseindia.com/Downloads1/Action_taken_against_trading_members.xls
       -> legacy .xls with disciplinary actions against BSE trading
          members. Non-canonical (no PPT slot); written under
          data/extras/ unless reclassified.

XLSX schema for #1 / #2 (verified May 2026):
  Date of Order/Email/Letter | Subject | Entity/Individual Name |
  PAN No. | Status | Start Date

XLS schema for #3 differs and is parsed leniently.

Convention
----------
- The download URL contains a date in DDMMYYYY form. The date used in
  the canonical pattern auto-rolls daily; we resolve it dynamically
  with a small head-of-week sweep (today, today-1d, ..., today-7d)
  rather than hard-coding so the scraper survives a daily re-build.
- requests + pandas + xlrd, per the same exception granted to
  sebi_enforcement_orders.py (see PRD §10 deviation note there).

Public functions
----------------
run()                       - run all 3
run_sebi_debarred_109()     - just SEBI Debarred (#109)
run_other_authorities_110() - just Other Competent Authorities (#110)
run_action_trading_members() - just BSE Action vs Trading Members
"""

import csv
import io
import os
import re
import sys
import zipfile
from datetime import datetime, timedelta

import pandas as pd
import requests
import urllib3
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR     = os.path.join(PROJECT_ROOT, "data")
EXTRAS_DIR   = os.path.join(PROJECT_ROOT, "data", "extras")

CSV_FIELDS = [
    "source_agency", "source_list", "case_unit",
    "name", "father_name", "date_of_birth", "gender",
    "address", "reward_amount", "details",
    "has_document", "document_url", "detail_page_url",
    "interpol_notice_id", "link_kind", "scraped_at",
    "enrichment_status",
]

UA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")
HEADERS = {"User-Agent": UA, "Referer": "https://www.bseindia.com/",
           "Accept": "*/*", "Accept-Language": "en-US,en;q=0.9"}

DEBARRED_PAGE = ("https://www.bseindia.com/markets/MarketInfo/"
                 "DispNewNoticesCirculars.aspx?page=Debarred%20Entities")
TM_ACTION_URL = ("https://www.bseindia.com/Downloads1/"
                 "Action_taken_against_trading_members.xls")

OUTPUT_FILE = os.path.join(DATA_DIR, "bse_debarred_sebi_109.csv")


def _session():
    s = requests.Session()
    retry = Retry(total=4, backoff_factor=1.5,
                  status_forcelist=(500, 502, 503, 504),
                  allowed_methods=("GET", "POST"),
                  raise_on_status=False)
    a = HTTPAdapter(max_retries=retry)
    s.mount("https://", a)
    s.mount("http://", a)
    s.headers.update(HEADERS)
    return s


# --------------------------------------------------------------------------
# Date-rolling URL resolver
# --------------------------------------------------------------------------
def _resolve_zip_url(session, prefix):
    """Given e.g. 'SEBI DEBARRED', try last 7 days and return the first
    URL that returns a valid ZIP."""
    base = "https://www.bseindia.com/download/DebarredEntities/"
    today = datetime.now()
    for delta in range(8):
        d = today - timedelta(days=delta)
        slug = d.strftime("%d%m%Y")
        url = f"{base}{prefix}%20{slug}.zip"
        try:
            r = session.head(url, timeout=15, verify=False,
                             allow_redirects=True)
        except requests.RequestException:
            continue
        if r.status_code == 200 and \
           "zip" in (r.headers.get("content-type") or "").lower():
            return url
    return None


# --------------------------------------------------------------------------
# Generic per-row mapper for the BSE debarred XLSX schema
# --------------------------------------------------------------------------
def _xlsx_rows_to_records(df, agency, list_name, link_kind, document_url,
                          scraped_at, header_row_idx):
    """Map every data row of `df` (which has the BSE debarred schema)
    to the project's 17-column dict."""
    if df is None or df.empty:
        return []
    headers = [str(c).strip() if c is not None else "" for c in df.iloc[header_row_idx]]

    def col_idx(*aliases):
        for i, h in enumerate(headers):
            for a in aliases:
                if a.lower() in h.lower():
                    return i
        return None

    i_date     = col_idx("Date of Order", "Order Date", "Date")
    i_subject  = col_idx("Subject", "Order Particulars", "Particulars")
    i_name     = col_idx("Entity", "Individual", "Name")
    i_pan      = col_idx("PAN")
    i_din      = col_idx("DIN", "CIN")
    i_status   = col_idx("Status", "Period")
    i_start    = col_idx("Start Date")
    i_symbol   = col_idx("Symbol")

    out = []
    for r in df.iloc[header_row_idx + 1:].itertuples(index=False):
        cells = list(r)
        def cell(i):
            if i is None or i >= len(cells):
                return ""
            v = cells[i]
            if v is None or (isinstance(v, float) and pd.isna(v)):
                return ""
            return str(v).strip()

        name = cell(i_name)
        if not name:
            continue
        details = []
        for label, idx in (("Subject", i_subject),
                           ("PAN", i_pan), ("DIN/CIN", i_din),
                           ("Status", i_status), ("Period", i_status),
                           ("Start Date", i_start), ("Symbol", i_symbol)):
            v = cell(idx)
            if v:
                details.append(f"{label}: {v}")
        details.insert(0, f"Date: {cell(i_date)}")
        out.append({
            "source_agency":       agency,
            "source_list":         list_name,
            "case_unit":           cell(i_pan) or cell(i_din),
            "name":                name,
            "father_name":         "",
            "date_of_birth":       "",
            "gender":              "",
            "address":             "",
            "reward_amount":       "",
            "details":             " | ".join(details)[:1500],
            "has_document":        "Yes",
            "document_url":        document_url,
            "detail_page_url":     DEBARRED_PAGE,
            "interpol_notice_id":  "",
            "link_kind":           link_kind,
            "scraped_at":          scraped_at,
            "enrichment_status":   "none",
        })
    return out


def _find_header_row(df, must_contain=("name",)):
    for i in range(min(15, len(df))):
        joined = " ".join(str(c).lower() for c in df.iloc[i].dropna())
        if all(k in joined for k in must_contain):
            return i
    return 0


def _save_csv(records, out_path):
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    with open(out_path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=CSV_FIELDS)
        w.writeheader()
        for r in records:
            w.writerow({k: r.get(k, "") for k in CSV_FIELDS})
    print(f"  wrote {len(records)} rows to {out_path}")


def _save_recipe(recipe_id, url, notes):
    try:
        from utils.request_recipes import save_recipe
        save_recipe(recipe_id, {
            "source_id": None,
            "url": url, "method": "GET",
            "headers": HEADERS, "params": {},
            "body": None, "cookies": {},
            "response_type": "binary",
            "notes": notes,
        })
    except Exception:
        pass


# --------------------------------------------------------------------------
# Per-source runners
# --------------------------------------------------------------------------
def _scrape_zip_xlsx(session, prefix, agency, list_name, link_kind,
                     out_csv_path):
    url = _resolve_zip_url(session, prefix)
    if url is None:
        print(f"  could not resolve {prefix} ZIP URL within last 7 days")
        return []
    print(f"  zip url: {url}")
    r = session.get(url, timeout=60, verify=False)
    if r.status_code != 200:
        print(f"  http {r.status_code}")
        return []
    z = zipfile.ZipFile(io.BytesIO(r.content))
    members = [m for m in z.namelist() if m.lower().endswith((".xlsx",".xls"))]
    if not members:
        print(f"  no xlsx in zip: {z.namelist()}")
        return []
    inner = members[0]
    df = pd.read_excel(io.BytesIO(z.read(inner)), sheet_name=0, header=None)
    hdr_idx = _find_header_row(df, must_contain=("name",))
    scraped_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    records = _xlsx_rows_to_records(df, agency, list_name, link_kind,
                                    url, scraped_at, hdr_idx)
    _save_csv(records, out_csv_path)
    _save_recipe(f"bse_debarred_{prefix.lower().replace(' ','_')}", url,
                 f"BSE debarred-entities ZIP — {prefix}. Daily-rolling URL.")
    return records


def run_sebi_debarred_109(session=None):
    print("[BSE] SEBI Debarred Entities (#109)")
    s = session or _session()
    out = os.path.join(DATA_DIR, "sebi_list_of_debarred_entities_109.csv")
    return _scrape_zip_xlsx(s, "SEBI DEBARRED",
                            agency="BSE",
                            list_name="List of Debarred Entities based on Orders / Directions",
                            link_kind="bse_debarred_sebi",
                            out_csv_path=out)


def run_other_authorities_110(session=None):
    print("[BSE] Other Competent Authorities Debarred (#110)")
    s = session or _session()
    out = os.path.join(DATA_DIR, "sebi_list_of_debarred_entities_110.csv")
    return _scrape_zip_xlsx(s, "Other Competent Authorities DEBARRED",
                            agency="BSE",
                            list_name="List of Debarred Entities Based on Orders / Directions from Other Competent Authorities",
                            link_kind="bse_debarred_other",
                            out_csv_path=out)


def run_action_trading_members(session=None):
    print("[BSE] Action Against Trading Members (legacy .xls)")
    s = session or _session()
    r = s.get(TM_ACTION_URL, timeout=60, verify=False)
    if r.status_code != 200:
        print(f"  http {r.status_code}")
        return []
    try:
        # Some BSE .xls files are HTML disguised; pandas with engine=xlrd
        # fails -> retry as html.
        df = pd.read_excel(io.BytesIO(r.content), sheet_name=0,
                           header=None, engine="xlrd")
    except Exception as e:
        print(f"  xlrd failed ({type(e).__name__}: {e}); trying html parse")
        try:
            tbls = pd.read_html(io.StringIO(r.text))
            if not tbls:
                return []
            df = tbls[0]
            df.columns = range(df.shape[1])
        except Exception as e2:
            print(f"  html parse also failed: {e2}")
            return []
    hdr_idx = _find_header_row(df, must_contain=("name",))
    scraped_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    records = _xlsx_rows_to_records(
        df, agency="BSE",
        list_name="Action Against Trading Members",
        link_kind="bse_action_trading_members",
        document_url=TM_ACTION_URL, scraped_at=scraped_at,
        header_row_idx=hdr_idx)
    out_path = os.path.join(EXTRAS_DIR,
                            "bse_action_against_trading_members.csv")
    _save_csv(records, out_path)
    _save_recipe("bse_action_trading_members", TM_ACTION_URL,
                 "BSE disciplinary actions vs trading members; legacy .xls")
    return records


def run():
    print("=" * 60)
    print("BSE Debarred Entities + Trading-Member Actions")
    print("=" * 60)
    s = _session()
    a = run_sebi_debarred_109(s)
    b = run_other_authorities_110(s)
    c = run_action_trading_members(s)
    s.close()
    print(f"\nSummary: SEBI debarred={len(a)}  other_auth={len(b)}  "
          f"trading_members={len(c)}")
    return {"sebi": a, "other": b, "trading_members": c}


if __name__ == "__main__":
    run()
