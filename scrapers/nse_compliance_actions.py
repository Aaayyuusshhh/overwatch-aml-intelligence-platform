"""
scrapers/nse_compliance_actions.py — NSE compliance / regulatory data
beyond the SEBI/Others debarred files (which live in
scrapers/nse_debarred_entities.py).

Six sources packaged into one module:

  1. NSE Non-Compliant Companies (Equity)        - SOP_E_Noncompliance.xls
  2. NSE Non-Compliant Promoter Freezing / Z     - dated .xlsx
  3. NSE ICDR Fines                              - dated .xls
  4. NSE Defaulting Clients (#240)               - .xlsx, maps to canonical
  5. NSE Members with Inadequate Networth        - HTML table on regs page
  6. NSE Authorized Persons Cancelled            - PDF download

The regulations landing page is JS-walled to Playwright (HTTP/2
RST_STREAM) but plain `requests` with a normal browser User-Agent is
served the full HTML. We exploit that.

Outputs (all 17-column schema)
------------------------------
data/nse_non_compliant_equity.csv
data/nse_promoter_freezing.csv
data/nse_icdr_fines.csv
data/nse_defaulting_clients_240.csv          (maps to canonical #240)
data/nse_members_inadequate_networth.csv
data/nse_authorized_persons_cancelled.csv

Public functions
----------------
run()                                  # all 6
run_non_compliant_equity()
run_promoter_freezing()
run_icdr_fines()
run_defaulting_clients_240()
run_members_inadequate_networth()
run_ap_cancelled()
"""

import csv
import io
import os
import re
from datetime import datetime
from urllib.parse import urljoin

import pandas as pd
import requests
import urllib3
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR     = os.path.join(PROJECT_ROOT, "data")
RAW_DIR      = os.path.join(PROJECT_ROOT, "data", "raw")

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
           "Accept-Language": "en-US,en;q=0.9"}

REG_PAGE = ("https://www.nseindia.com/regulations/"
            "exchange-market-surveillance-regulatory-actions")

URL_NONCOMPLIANT = ("https://nsearchives.nseindia.com/corporates/content/"
                    "SOP_E_Noncompliance.xls")
URL_PROMOTER_FREEZING = ("https://nsearchives.nseindia.com//web/mediaattachment/"
                         "2026-04/Noncompliant_companies__Promoter_freezing_"
                         "and_Movement_to_Z_22-04-2026_20260422163135.xlsx")
URL_ICDR_FINES = ("https://nsearchives.nseindia.com//web/mediaattachment/"
                  "2026-04/ICDR_Fines_17.04.2026_20260421161852.xls")
URL_DEFAULTING_CLIENTS = ("https://nsearchives.nseindia.com/web/sites/default/"
                          "files/inline-files/"
                          "Defaulting_Client_Database%202_1_1%20%281%29%20%281%29.xlsx")

OUTPUT_FILE = os.path.join(DATA_DIR, "nse_non_compliant_equity.csv")


# --------------------------------------------------------------------------
# Session + helpers
# --------------------------------------------------------------------------
def _session():
    s = requests.Session()
    retry = Retry(total=4, backoff_factor=1.5,
                  status_forcelist=(500, 502, 503, 504),
                  allowed_methods=("GET", "POST"),
                  raise_on_status=False)
    s.mount("https://", HTTPAdapter(max_retries=retry))
    s.headers.update(HEADERS)
    return s


def _save_csv(records, out_path):
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    with open(out_path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=CSV_FIELDS)
        w.writeheader()
        for r in records:
            w.writerow({k: r.get(k, "") for k in CSV_FIELDS})
    print(f"  wrote {len(records)} rows to {out_path}")


def _save_recipe(recipe_id, url, notes, response_type="binary"):
    try:
        from utils.request_recipes import save_recipe
        save_recipe(recipe_id, {
            "source_id": None,
            "url": url, "method": "GET",
            "headers": HEADERS, "params": {}, "body": None, "cookies": {},
            "response_type": response_type,
            "notes": notes,
        })
    except Exception:
        pass


def _find_header_row(df, must_contain=("name",)):
    """Locate the row that looks like the header (contains all keywords)."""
    for i in range(min(15, len(df))):
        joined = " ".join(str(c).lower() for c in df.iloc[i].dropna())
        if all(k in joined for k in must_contain):
            return i
    return 0


def _read_excel(content):
    """Try openpyxl then xlrd. Returns dataframe of first sheet, header=None."""
    for engine in ("openpyxl", "xlrd"):
        try:
            return pd.read_excel(io.BytesIO(content), sheet_name=0,
                                 header=None, engine=engine)
        except Exception:
            continue
    return None


def _cell(row, idx):
    if idx is None or idx >= len(row):
        return ""
    v = row[idx]
    if v is None or (isinstance(v, float) and pd.isna(v)):
        return ""
    s = str(v).strip()
    # Trim trailing midnight time on dates that pandas read as datetime.
    s = re.sub(r"\s+00:00:00$", "", s)
    return s


def _make_record(agency, list_name, link_kind, doc_url, scraped_at, *,
                 name, case_unit="", details_parts=None):
    if not name or len(name) < 2:
        return None
    details_parts = details_parts or []
    return {
        "source_agency": agency,
        "source_list":   list_name,
        "case_unit":     case_unit,
        "name":          name,
        "father_name":   "",
        "date_of_birth": "",
        "gender":        "",
        "address":       "",
        "reward_amount": "",
        "details":       " | ".join(p for p in details_parts if p)[:1500],
        "has_document":  "Yes" if doc_url else "No",
        "document_url":  doc_url or "",
        "detail_page_url": REG_PAGE,
        "interpol_notice_id": "",
        "link_kind":     link_kind,
        "scraped_at":    scraped_at,
        "enrichment_status": "none",
    }


# --------------------------------------------------------------------------
# 1. Non-compliant equity
# --------------------------------------------------------------------------
def run_non_compliant_equity(session=None):
    print("[NSE] Non-Compliant Companies (Equity)")
    s = session or _session()
    r = s.get(URL_NONCOMPLIANT, timeout=60, verify=False)
    if r.status_code != 200:
        print(f"  http {r.status_code}")
        return []
    df = _read_excel(r.content)
    if df is None or df.empty:
        print("  could not parse")
        return []
    hdr_idx = _find_header_row(df, must_contain=("company",))
    headers = [str(c).strip() for c in df.iloc[hdr_idx]]

    def col(*aliases):
        for i, h in enumerate(headers):
            for a in aliases:
                if a.lower() in h.lower():
                    return i
        return None

    i_sno     = col("Sr.No", "S.No")
    i_symbol  = col("Symbol")
    i_name    = col("Company Name", "Company")
    i_qtr     = col("Quarter")
    i_reg     = col("Regulation")
    i_due     = col("Due date")
    i_done    = col("Date of Compliance")
    i_fine    = col("Fine")

    scraped_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    out = []
    for r in df.iloc[hdr_idx + 1:].itertuples(index=False):
        cells = list(r)
        name = _cell(cells, i_name)
        if not name:
            continue
        details = [
            f"Symbol: {_cell(cells, i_symbol)}",
            f"Quarter: {_cell(cells, i_qtr)}",
            f"Regulation: {_cell(cells, i_reg)}",
            f"Due: {_cell(cells, i_due)}",
            f"Compliance: {_cell(cells, i_done)}",
            f"Fine: {_cell(cells, i_fine)}",
        ]
        rec = _make_record(
            "NSE", "Non-Compliant Companies (Equity)",
            "nse_non_compliant_equity", URL_NONCOMPLIANT, scraped_at,
            name=name, case_unit=_cell(cells, i_sno),
            details_parts=details)
        if rec:
            out.append(rec)
    out_path = os.path.join(DATA_DIR, "nse_non_compliant_equity.csv")
    _save_csv(out, out_path)
    _save_recipe("nse_non_compliant_equity", URL_NONCOMPLIANT,
                 "NSE Non-Compliant Companies (Equity) — daily Excel dump")
    return out


# --------------------------------------------------------------------------
# 2. Promoter Freezing & Movement to Z
# --------------------------------------------------------------------------
def run_promoter_freezing(session=None):
    print("[NSE] Non-Compliant Promoter Freezing / Z")
    s = session or _session()
    r = s.get(URL_PROMOTER_FREEZING, timeout=60, verify=False)
    if r.status_code != 200:
        print(f"  http {r.status_code}")
        return []
    df = _read_excel(r.content)
    if df is None or df.empty:
        print("  could not parse")
        return []
    # Header has duplicated 'Symbol' label — fall back to row index 0.
    hdr_idx = 0
    headers = [str(c).strip() for c in df.iloc[hdr_idx]]
    # Schema: Sr.No | Symbol(ticker) | Symbol(name) | Quarter | Regulation
    #         Date of Freezing | Date of Unfreezing | Date of Movement to Z
    scraped_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    out = []
    for r in df.iloc[hdr_idx + 1:].itertuples(index=False):
        cells = list(r)
        name = _cell(cells, 2)  # second 'Symbol' col is the company name
        if not name:
            continue
        details = [
            f"Symbol: {_cell(cells, 1)}",
            f"Quarter: {_cell(cells, 3)}",
            f"Regulation: {_cell(cells, 4)}",
            f"Date of Freezing: {_cell(cells, 5)}",
            f"Date of Unfreezing: {_cell(cells, 6)}",
            f"Date of Movement to Z: {_cell(cells, 7)}",
        ]
        rec = _make_record(
            "NSE", "Non-Compliant Promoter Freezing / Z Movement",
            "nse_promoter_freezing", URL_PROMOTER_FREEZING, scraped_at,
            name=name, case_unit=_cell(cells, 0),
            details_parts=details)
        if rec:
            out.append(rec)
    out_path = os.path.join(DATA_DIR, "nse_promoter_freezing.csv")
    _save_csv(out, out_path)
    _save_recipe("nse_promoter_freezing", URL_PROMOTER_FREEZING,
                 "NSE Non-Compliant Promoter Freezing & Movement-to-Z list")
    return out


# --------------------------------------------------------------------------
# 3. ICDR Fines
# --------------------------------------------------------------------------
def run_icdr_fines(session=None):
    print("[NSE] ICDR Fines")
    s = session or _session()
    r = s.get(URL_ICDR_FINES, timeout=60, verify=False)
    if r.status_code != 200:
        print(f"  http {r.status_code}")
        return []
    df = _read_excel(r.content)
    if df is None or df.empty:
        print("  could not parse")
        return []
    hdr_idx = _find_header_row(df, must_contain=("name",))
    headers = [str(c).strip() for c in df.iloc[hdr_idx]]

    def col(*aliases):
        for i, h in enumerate(headers):
            for a in aliases:
                if a.lower() in h.lower():
                    return i
        return None

    i_reg     = col("Regulation")
    i_nature  = col("Nature of Violation")
    i_symbol  = col("Symbol")
    i_name    = col("Name of the Company", "Name")
    i_date    = col("Date of  Fine Impose", "Date of Fine", "Review Date")
    i_amt     = col("Amount of Fine as on Review", "Amount of Fine")
    i_recv    = col("Date of  receipt", "receipt of Fine")
    i_recvamt = col("Amount of Fine received")

    scraped_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    out = []
    for r in df.iloc[hdr_idx + 1:].itertuples(index=False):
        cells = list(r)
        name = _cell(cells, i_name)
        if not name:
            continue
        details = [
            f"Symbol: {_cell(cells, i_symbol)}",
            f"Regulations violated: {_cell(cells, i_reg)}",
            f"Nature: {_cell(cells, i_nature)}",
            f"Date of Fine: {_cell(cells, i_date)}",
            f"Fine Amount: {_cell(cells, i_amt)}",
            f"Date of Receipt: {_cell(cells, i_recv)}",
            f"Receipt Amount: {_cell(cells, i_recvamt)}",
        ]
        rec = _make_record(
            "NSE", "ICDR Fines",
            "nse_icdr_fines", URL_ICDR_FINES, scraped_at,
            name=name,
            case_unit=_cell(cells, i_symbol),
            details_parts=details)
        if rec:
            out.append(rec)
    out_path = os.path.join(DATA_DIR, "nse_icdr_fines.csv")
    _save_csv(out, out_path)
    _save_recipe("nse_icdr_fines", URL_ICDR_FINES,
                 "NSE ICDR Fines — list of companies non-compliant with SEBI ICDR")
    return out


# --------------------------------------------------------------------------
# 4. Defaulting Clients (#240, canonical)
# --------------------------------------------------------------------------
def run_defaulting_clients_240(session=None):
    print("[NSE] Defaulting Clients (#240)")
    s = session or _session()
    r = s.get(URL_DEFAULTING_CLIENTS, timeout=60, verify=False)
    if r.status_code != 200:
        print(f"  http {r.status_code}")
        return []
    df = _read_excel(r.content)
    if df is None or df.empty:
        print("  could not parse")
        return []
    hdr_idx = _find_header_row(df, must_contain=("defaulting", "client"))
    headers = [str(c).strip() for c in df.iloc[hdr_idx]]

    def col(*aliases):
        for i, h in enumerate(headers):
            for a in aliases:
                if a.lower() in h.lower():
                    return i
        return None

    i_sno   = col("S. No", "Sr.No", "S.No")
    i_name  = col("Defaulting client", "Defaulting Client")
    i_pan   = col("Pan", "PAN")
    i_tm    = col("trading member", "Trading Member")
    i_case  = col("Complaint No", "Arbitration", "Case")
    i_date  = col("Date of Order", "Date of Award")
    i_award = col("Award details", "Award")
    i_exch  = col("Exchange")

    scraped_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    out = []
    for r in df.iloc[hdr_idx + 1:].itertuples(index=False):
        cells = list(r)
        name = _cell(cells, i_name)
        if not name:
            continue
        details = [
            f"PAN: {_cell(cells, i_pan)}",
            f"Trading Member: {_cell(cells, i_tm)}",
            f"Case: {_cell(cells, i_case)}",
            f"Date of Order: {_cell(cells, i_date)}",
            f"Award: {_cell(cells, i_award)}",
            f"Exchange: {_cell(cells, i_exch)}",
        ]
        rec = _make_record(
            "NSE", "Defaulting Clients",
            "nse_defaulting_clients", URL_DEFAULTING_CLIENTS, scraped_at,
            name=name, case_unit=_cell(cells, i_pan),
            details_parts=details)
        if rec:
            out.append(rec)
    out_path = os.path.join(DATA_DIR, "nse_defaulting_clients_240.csv")
    _save_csv(out, out_path)
    _save_recipe("nse_defaulting_clients_240", URL_DEFAULTING_CLIENTS,
                 "NSE Defaulting Clients Database (canonical #240)")
    return out


# --------------------------------------------------------------------------
# 5. Members with Inadequate Networth (HTML table on regs page)
# --------------------------------------------------------------------------
def _fetch_regulations_page(session):
    r = session.get(REG_PAGE, timeout=45, verify=False)
    if r.status_code != 200:
        print(f"  regulations page http {r.status_code}")
        return None
    return r.text


def run_members_inadequate_networth(session=None, html_text=None):
    print("[NSE] Members with Inadequate Networth")
    s = session or _session()
    text = html_text if html_text is not None else _fetch_regulations_page(s)
    if not text:
        return []
    # Find heading + the immediately-following <table>.
    idx = text.find("Members with Inadequate Networth")
    if idx < 0:
        print("  heading not found")
        return []
    chunk = text[idx:idx + 60_000]
    tables = re.findall(r"<table[^>]*>([\s\S]*?)</table>", chunk, re.I)
    if not tables:
        print("  no table after heading")
        return []
    # First table after the heading has Sr.No / Name / SEBI Reg / Networth / Action.
    trs = re.findall(r"<tr[^>]*>([\s\S]*?)</tr>", tables[0], re.I)
    if len(trs) < 2:
        return []
    scraped_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    out = []
    for tr in trs[1:]:
        cells = re.findall(r"<t[dh][^>]*>([\s\S]*?)</t[dh]>", tr, re.I)
        clean = [re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", c)).strip()
                 for c in cells]
        if len(clean) < 4:
            continue
        sno, name, sebi_reg, min_nw = clean[0], clean[1], clean[2], clean[3]
        meeting = clean[4] if len(clean) > 4 else ""
        action  = clean[5] if len(clean) > 5 else ""
        if not name:
            continue
        rec = _make_record(
            "NSE", "Members with Inadequate Networth",
            "nse_inadequate_networth", REG_PAGE, scraped_at,
            name=name, case_unit=sebi_reg,
            details_parts=[
                f"S.No: {sno}",
                f"SEBI Registration: {sebi_reg}",
                f"Min Networth Req (Rs Cr): {min_nw}",
                f"Meeting Min Networth: {meeting}",
                f"Action taken: {action}",
            ])
        if rec:
            out.append(rec)
    out_path = os.path.join(DATA_DIR, "nse_members_inadequate_networth.csv")
    _save_csv(out, out_path)
    _save_recipe("nse_members_inadequate_networth", REG_PAGE,
                 "NSE Members with Inadequate Networth — HTML table on "
                 "regulations/exchange-market-surveillance page",
                 response_type="html")
    return out


# --------------------------------------------------------------------------
# 6. Authorized Persons Cancelled (PDF)
# --------------------------------------------------------------------------
def _discover_ap_pdf(session, html_text=None):
    """Discover the dated AP-cancelled PDF URL from the regulations page."""
    text = html_text if html_text is not None else _fetch_regulations_page(session)
    if not text:
        return None
    m = re.search(r'''href=["']([^"']*List_of_AP_Cancelled[^"']*\.pdf)["']''',
                  text, re.I)
    if not m:
        return None
    href = m.group(1).strip()
    # Some hrefs have leading double-slash; normalise.
    href = re.sub(r"//+", "//", href.replace("https://", "")).lstrip("/")
    return "https://" + href if not href.startswith("http") else href


def run_ap_cancelled(session=None, html_text=None):
    print("[NSE] Authorized Persons Cancelled (PDF)")
    s = session or _session()
    pdf_url = _discover_ap_pdf(s, html_text)
    if not pdf_url:
        print("  could not discover PDF URL")
        return []
    print(f"  pdf url: {pdf_url}")
    # Use the project's PDF engine for consistent extraction.
    try:
        from engines import pdf_scraper
    except Exception as e:
        print(f"  pdf_scraper import failed: {e}")
        return []
    sub_source = {
        "id":   "nse_authorized_persons_cancelled",
        "agency": "NSE",
        "list_name": "Authorized Persons Cancelled by Trading Member due to Disciplinary Reason",
        "url":  pdf_url,
        "type": "pdf",
    }
    res = pdf_scraper.run(sub_source)
    out = []
    if res.get("status") == "success" and res.get("csv_path"):
        # Re-read the engine's CSV, fix source_agency = NSE (engine wrote
        # whatever was in `agency`), retag link_kind, restamp.
        scraped_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        with open(res["csv_path"], "r", encoding="utf-8", newline="") as f:
            rows = list(csv.DictReader(f))
        for r in rows:
            r["source_agency"] = "NSE"
            r["source_list"]   = "Authorized Persons Cancelled by Trading Member due to Disciplinary Reason"
            r["link_kind"]     = "nse_ap_cancelled"
            r["detail_page_url"] = REG_PAGE
            r["scraped_at"]    = scraped_at
            r["document_url"]  = pdf_url
            out.append(r)
    out_path = os.path.join(DATA_DIR, "nse_authorized_persons_cancelled.csv")
    _save_csv(out, out_path)
    _save_recipe("nse_authorized_persons_cancelled", pdf_url,
                 "NSE Authorized Persons Cancelled by Trading Member — "
                 "PDF, URL discovered from regulations page",
                 response_type="binary")
    return out


# --------------------------------------------------------------------------
# Run all 6
# --------------------------------------------------------------------------
def run():
    print("=" * 60)
    print("NSE compliance / regulatory actions — 6 sources")
    print("=" * 60)
    s = _session()
    summary = {}
    summary["non_compliant_equity"] = run_non_compliant_equity(s)
    summary["promoter_freezing"]    = run_promoter_freezing(s)
    summary["icdr_fines"]           = run_icdr_fines(s)
    summary["defaulting_clients"]   = run_defaulting_clients_240(s)
    # Fetch the regulations page once and reuse for #5 + #6.
    page_html = _fetch_regulations_page(s)
    summary["inadequate_networth"]  = run_members_inadequate_networth(
        s, html_text=page_html)
    summary["ap_cancelled"]         = run_ap_cancelled(s, html_text=page_html)
    s.close()
    print("\nPer-source summary:")
    for k, v in summary.items():
        print(f"  {k:<25} {len(v):>6}")
    return summary


if __name__ == "__main__":
    run()
