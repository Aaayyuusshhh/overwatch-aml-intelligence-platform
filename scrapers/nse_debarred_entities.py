"""
scrapers/nse_debarred_entities.py — NSE Debarred Entities (SEBI / Others).

Two direct .xls files on the NSE archives bucket:

  https://nsearchives.nseindia.com/content/press/prs_ra_sebi.xls   (~1 MB)
  https://nsearchives.nseindia.com/content/press/prs_ra_others.xls (~330 KB)

Despite the .xls extension, the files served by NSE are actually
modern OOXML (read by pandas with engine=openpyxl). Schema verified
May 2026:

  Order Date | Order Particulars | Entity / Individual Name | PAN |
  DIN / CIN | Symbol | Period | (extra trailing columns)

Public functions
----------------
run()                      - both files
run_sebi()                 - just /prs_ra_sebi.xls
run_others()               - just /prs_ra_others.xls

Output (non-canonical, written to data/extras/ unless reclassified):
  data/nse_debarred_sebi.csv
  data/nse_debarred_others.csv
"""

import csv
import io
import os
from datetime import datetime

import pandas as pd
import requests
import urllib3
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR     = os.path.join(PROJECT_ROOT, "data")

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
HEADERS = {"User-Agent": UA, "Referer": "https://www.nseindia.com/",
           "Accept": "*/*", "Accept-Language": "en-US,en;q=0.9"}

URL_SEBI   = "https://nsearchives.nseindia.com/content/press/prs_ra_sebi.xls"
URL_OTHERS = "https://nsearchives.nseindia.com/content/press/prs_ra_others.xls"
DETAIL_PAGE = ("https://www.nseindia.com/regulations/"
               "exchange-debarred-entities")

OUTPUT_FILE = os.path.join(DATA_DIR, "nse_debarred_sebi.csv")


def _session():
    s = requests.Session()
    retry = Retry(total=4, backoff_factor=1.5,
                  status_forcelist=(500, 502, 503, 504),
                  allowed_methods=("GET",), raise_on_status=False)
    s.mount("https://", HTTPAdapter(max_retries=retry))
    s.headers.update(HEADERS)
    return s


def _find_header_row(df, must_contain=("name",)):
    for i in range(min(15, len(df))):
        joined = " ".join(str(c).lower() for c in df.iloc[i].dropna())
        if all(k in joined for k in must_contain):
            return i
    return 0


def _rows_to_records(df, agency, list_name, link_kind, doc_url, scraped_at):
    if df is None or df.empty:
        return []
    hdr_idx = _find_header_row(df, must_contain=("name",))
    headers = [str(c).strip() if c is not None else "" for c in df.iloc[hdr_idx]]

    def col(*aliases):
        for i, h in enumerate(headers):
            for a in aliases:
                if a.lower() in h.lower():
                    return i
        return None

    i_date    = col("Order Date", "Date")
    i_subj    = col("Order Particulars", "Particulars")
    i_name    = col("Entity", "Individual", "Name")
    i_pan     = col("PAN")
    i_dincin  = col("DIN", "CIN")
    i_symbol  = col("Symbol")
    i_period  = col("Period")
    i_circ    = col("Circular", "NSE/")  # the trailing reference column

    out = []
    for r in df.iloc[hdr_idx + 1:].itertuples(index=False):
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
        for label, idx in (("Order Particulars", i_subj),
                           ("PAN", i_pan), ("DIN/CIN", i_dincin),
                           ("Symbol", i_symbol), ("Period", i_period),
                           ("Circular", i_circ)):
            v = cell(idx)
            if v and v not in ("-", "ALL"):
                details.append(f"{label}: {v}")
        details.insert(0, f"Date: {cell(i_date)}")
        out.append({
            "source_agency":       agency,
            "source_list":         list_name,
            "case_unit":           cell(i_pan) or cell(i_dincin),
            "name":                name,
            "father_name":         "",
            "date_of_birth":       "",
            "gender":              "",
            "address":             "",
            "reward_amount":       "",
            "details":             " | ".join(details)[:1500],
            "has_document":        "Yes",
            "document_url":        doc_url,
            "detail_page_url":     DETAIL_PAGE,
            "interpol_notice_id":  "",
            "link_kind":           link_kind,
            "scraped_at":          scraped_at,
            "enrichment_status":   "none",
        })
    return out


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


def _scrape(session, url, agency, list_name, link_kind, out_filename):
    r = session.get(url, timeout=60, verify=False)
    if r.status_code != 200:
        print(f"  http {r.status_code} for {url}")
        return []
    # NSE serves OOXML despite .xls extension; openpyxl handles it.
    try:
        df = pd.read_excel(io.BytesIO(r.content), sheet_name=0, header=None,
                           engine="openpyxl")
    except Exception as e:
        print(f"  openpyxl failed ({type(e).__name__}: {e}); trying xlrd")
        df = pd.read_excel(io.BytesIO(r.content), sheet_name=0, header=None,
                           engine="xlrd")
    scraped_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    records = _rows_to_records(df, agency, list_name, link_kind, url,
                               scraped_at)
    out = os.path.join(DATA_DIR, out_filename)
    _save_csv(records, out)
    _save_recipe(f"nse_debarred_{link_kind.split('_')[-1]}", url,
                 f"NSE debarred-entities Excel ({link_kind}). Direct download.")
    return records


def run_sebi(session=None):
    print("[NSE] Debarred Entities — SEBI Orders")
    s = session or _session()
    return _scrape(s, URL_SEBI, "NSE",
                   "Debarred Entities (SEBI Orders)",
                   "nse_debarred_sebi", "nse_debarred_sebi.csv")


def run_others(session=None):
    print("[NSE] Debarred Entities — Other Authorities")
    s = session or _session()
    return _scrape(s, URL_OTHERS, "NSE",
                   "Debarred Entities (Other Competent Authorities)",
                   "nse_debarred_others", "nse_debarred_others.csv")


def run():
    print("=" * 60)
    print("NSE Debarred Entities")
    print("=" * 60)
    s = _session()
    a = run_sebi(s)
    b = run_others(s)
    s.close()
    print(f"\nSummary: NSE SEBI={len(a)}  NSE others={len(b)}")
    return {"sebi": a, "others": b}


if __name__ == "__main__":
    run()
