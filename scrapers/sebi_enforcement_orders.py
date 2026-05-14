"""
scrapers/sebi_enforcement_orders.py — SEBI Enforcement Orders.

Posts to the AJAX endpoint that powers the Enforcement → Orders pages
on sebi.gov.in:

    POST https://www.sebi.gov.in/sebiweb/ajax/home/getnewslistinfo.jsp

The endpoint returns a fragment of HTML containing the data table for
one page (25 rows by default) plus hidden inputs that disclose the
total number of pages. We paginate by incrementing nextValue from 0
to totalpage-1 (server is 0-indexed) and parse each page's <table>.

Eight enforcement sub-lists are scraped under the same endpoint, each
identified by a `smid` value:

    smid  smText (display)                        approx records
       1  Orders of SAT
       2  Orders of Chairperson/Members           6,388
       3  Settlement Order
       6  Orders of AO
       7  Orders of Courts
      77  Orders Of Special Courts
     133  Orders of ED / CGM
     138  Orders under Regulation 30A

Output
------
data/sebi_enforcement_orders.csv          - combined, deduplicated by URL
data/sebi_orders_chairperson_member_115.csv  - smid 2  (maps to ppt #115)
data/sebi_settlement_orders_116.csv          - smid 3  (maps to ppt #116)
data/sebi_orders_of_ao_119.csv               - smid 6  (maps to ppt #119)
data/sebi_orders_<smid>.csv                  - other smids

CSV is the project's standard 17-column schema (PRD §7).

Deviation from PRD §10
----------------------
PRD §10 mandates Scrapling for HTTP/HTML. This scraper uses requests +
BeautifulSoup + pandas at the engineer's explicit direction. Reason:
SEBI's AJAX endpoint expects a precise XHR signature (X-Requested-With
header + form-encoded payload) that is more transparent to construct
with requests' Session API. The discovered request is also persisted
as a recipe under recipes/sebi_enforcement_orders_*.json so the
api_replay engine can drive future runs without this scraper's code
path.
"""

import csv
import os
import re
import sys
import time
import urllib3
from datetime import datetime
from urllib.parse import urljoin

import pandas as pd
import requests
from bs4 import BeautifulSoup

# Order titles like "Adjudication Order in respect of X in the matter of Y"
# need the entity (X / Y) lifted out of the title for screening to be
# usable. The cleaner is the same one previously run as a post-processing
# pass over the CSVs (scripts/sebi_name_cleaner.py); wiring it into the
# scraper avoids a second pass.
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)
from scripts.sebi_name_cleaner import extract_entity_name as _extract_entity_name
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

# Disable the "InsecureRequestWarning" once for the whole module — SEBI
# certificates have been intermittently mis-served, the codebase has
# verify=False as a project-wide convention.
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR     = os.path.join(PROJECT_ROOT, "data")

ENDPOINT = "https://www.sebi.gov.in/sebiweb/ajax/home/getnewslistinfo.jsp"
ENFORCE_PAGE = "https://www.sebi.gov.in/enforcement.html"

CSV_FIELDS = [
    "source_agency", "source_list", "case_unit",
    "name", "father_name", "date_of_birth", "gender",
    "address", "reward_amount", "details",
    "has_document", "document_url", "detail_page_url",
    "interpol_notice_id", "link_kind", "scraped_at",
    "enrichment_status",
]

SUB_LISTS = [
    # smid, smText, list_name, output filename, source_id
    (  2, "Orders of Chairperson/Members", "Orders of Chairperson/Members",
       "sebi_orders_of_chairperson_member_115.csv",
       "sebi_orders_of_chairperson_member_115"),
    (  3, "Settlement Order", "Settlement Orders",
       "sebi_settlement_orders_116.csv",
       "sebi_settlement_orders_116"),
    (  6, "Orders of AO", "Orders of AO",
       "sebi_orders_of_ao_119.csv",
       "sebi_orders_of_ao_119"),
    (  1, "Orders of SAT", "Orders of SAT",
       "sebi_orders_of_sat.csv", None),
    (  7, "Orders of Courts", "Orders of Courts",
       "sebi_orders_of_courts.csv", None),
    ( 77, "Orders Of Special Courts", "Orders of Special Courts",
       "sebi_orders_of_special_courts.csv", None),
    (133, "Orders of ED / CGM", "Orders of ED / CGM",
       "sebi_orders_of_ed_cgm.csv", None),
    (138, "Orders under Regulation 30A", "Orders under Regulation 30A",
       "sebi_orders_regulation_30a.csv", None),
]

COMBINED_OUT = os.path.join(DATA_DIR, "sebi_enforcement_orders.csv")
OUTPUT_FILE  = COMBINED_OUT  # required by handlers/html_handler dispatch

DEFAULT_HEADERS = {
    "User-Agent": ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"),
    "X-Requested-With": "XMLHttpRequest",
    "Referer": ENFORCE_PAGE,
    "Origin":  "https://www.sebi.gov.in",
    "Accept":  "*/*",
    "Accept-Language": "en-US,en;q=0.9",
    "Content-Type": "application/x-www-form-urlencoded",
}

POLITENESS_SECONDS = 0.5
PAGE_TIMEOUT       = 30
MAX_PAGES_HARDCAP  = 1_500   # safety net per smid
DEDUP_KEY          = ("url", "title", "date")


# ---------------------------------------------------------------------------
# HTTP session with retry
# ---------------------------------------------------------------------------
def _build_session():
    s = requests.Session()
    retry = Retry(
        total=4,
        backoff_factor=1.5,
        status_forcelist=(500, 502, 503, 504, 522, 524),
        allowed_methods=("GET", "POST"),
        raise_on_status=False,
    )
    adapter = HTTPAdapter(max_retries=retry, pool_connections=10,
                          pool_maxsize=10)
    s.mount("https://", adapter)
    s.mount("http://", adapter)
    s.headers.update(DEFAULT_HEADERS)
    return s


# ---------------------------------------------------------------------------
# Page fetch + parse
# ---------------------------------------------------------------------------
def _build_payload(smid, smtext, next_value, *,
                   ssid="9", sstext="Orders"):
    """Build the form-encoded payload for one page request.

    The default (ssid='9', sstext='Orders') hits the Enforcement→Orders
    sub-section that owns smids 1/2/3/6/7/77/133/138. Pass alternate
    ssid + sstext for the smid=0 sub-lists under different ssTexts:
    Recovery Proceedings (50), Auction Notice (79), Unserved
    Summons/Notices (13), Orders That Could Not be Served (12)."""
    return {
        "nextValue": str(next_value),
        "next": "n",
        "search": "",
        "fromDate": "",
        "toDate": "",
        "fromYear": "",
        "toYear": "",
        "deptId": "-1",
        "sid": "2",
        "ssid": str(ssid),
        "smid": str(smid),
        "ssidhidden": str(ssid),
        "intmid": "-1",
        "sText": "Enforcement",
        "ssText": sstext,
        "smText": smtext,
        "doDirect": "-1",
    }


def _fetch_page(session, smid, smtext, next_value, *,
                ssid="9", sstext="Orders"):
    """POST one page. Returns (html, totalpage) or (None, None) on failure."""
    payload = _build_payload(smid, smtext, next_value, ssid=ssid, sstext=sstext)
    try:
        resp = session.post(ENDPOINT, data=payload, timeout=PAGE_TIMEOUT,
                            verify=False)
    except requests.RequestException as e:
        print(f"  ssid={ssid} smid={smid} page={next_value}: request error "
              f"{type(e).__name__}: {str(e)[:120]}")
        return None, None
    if resp.status_code != 200:
        print(f"  ssid={ssid} smid={smid} page={next_value}: http {resp.status_code}")
        return None, None
    text = resp.text
    m = re.search(r"name=['\"]totalpage['\"][^>]*value=(\d+)", text)
    totalpage = int(m.group(1)) if m else None
    return text, totalpage


def _parse_rows(html, smid, smtext, list_name, scraped_at):
    """Return list[dict] in the 17-col schema for this page."""
    soup = BeautifulSoup(html, "html.parser")
    out = []
    for tr in soup.select("table tr"):
        tds = tr.find_all("td")
        if len(tds) < 2:
            continue
        date_text  = tds[0].get_text(strip=True)
        title_cell = tds[1]
        a = title_cell.find("a")
        title = title_cell.get_text(strip=True)
        url = ""
        if a and a.get("href"):
            url = urljoin("https://www.sebi.gov.in/", a["href"].strip())
        if not title:
            continue
        # Extract the actual entity from the order title; keep the
        # original verbatim title in `details` so nothing is lost.
        extracted_name, _pattern = _extract_entity_name(title)
        if not extracted_name or len(extracted_name) < 3:
            extracted_name = title
        if extracted_name != title:
            details_str = (f"Original title: {title} | "
                            f"Date: {date_text} | smid: {smid} ({smtext})")
        else:
            details_str = f"Date: {date_text} | smid: {smid} ({smtext})"
        out.append({
            "source_agency": "SEBI",
            "source_list":   list_name,
            "case_unit":     "",
            "name":          extracted_name,
            "father_name":   "",
            "date_of_birth": "",
            "gender":        "",
            "address":       "",
            "reward_amount": "",
            "details":       details_str,
            "has_document":  "Yes" if url else "No",
            "document_url":  url,
            "detail_page_url": ENFORCE_PAGE,
            "interpol_notice_id": "",
            "link_kind":     "sebi_enforcement_order",
            "scraped_at":    scraped_at,
            "enrichment_status": "none",
        })
    return out


# ---------------------------------------------------------------------------
# Per-smid driver
# ---------------------------------------------------------------------------
def _scrape_smid(session, smid, smtext, list_name, scraped_at,
                 max_pages=MAX_PAGES_HARDCAP, verbose=True,
                 ssid="9", sstext="Orders"):
    """Iterate every page for one smid (or ssid-only sub-list when
    smid=0); returns list of records. Stops on the first empty page or
    when nextValue >= totalpage."""
    out = []
    seen_url = set()
    seen_title_date = set()
    next_value = 0
    totalpage = None
    pages_done = 0
    label = f"ssid={ssid}/smid={smid}"

    while pages_done < max_pages:
        html, tp = _fetch_page(session, smid, smtext, next_value,
                               ssid=ssid, sstext=sstext)
        if html is None:
            break
        if totalpage is None and tp is not None:
            totalpage = tp
            if verbose:
                print(f"  {label} ({sstext or smtext}): totalpage={totalpage}")

        page_rows = _parse_rows(html, smid, smtext or sstext, list_name, scraped_at)

        # Per-page dedup (paranoia: server occasionally repeats a row at
        # page boundaries) AND cross-smid dedup by url.
        new_this_page = 0
        for r in page_rows:
            url_key = (r["document_url"] or "").lower()
            td_key  = (r["name"].lower(), r["details"].split("|", 1)[0])
            if url_key and url_key in seen_url:
                continue
            if not url_key and td_key in seen_title_date:
                continue
            if url_key:
                seen_url.add(url_key)
            seen_title_date.add(td_key)
            out.append(r)
            new_this_page += 1

        if verbose:
            print(f"  {label} page={next_value:>3} "
                  f"records_this_page={len(page_rows)} new={new_this_page} "
                  f"total_so_far={len(out)}")

        if new_this_page == 0:
            # Either we hit a duplicate-only page or there's nothing left.
            break

        pages_done += 1
        next_value += 1
        if totalpage is not None and next_value >= totalpage:
            break
        time.sleep(POLITENESS_SECONDS)

    return out


# ---------------------------------------------------------------------------
# Save
# ---------------------------------------------------------------------------
def _save_csv(records, path):
    if not records:
        # write header-only file so combine.py / load_db can still see it
        with open(path, "w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=CSV_FIELDS)
            w.writeheader()
        print(f"  wrote 0 rows to {path}")
        return 0
    df = pd.DataFrame(records, columns=CSV_FIELDS)
    df.to_csv(path, index=False)
    print(f"  wrote {len(df)} rows to {path}")
    return len(df)


def _save_recipe(smid, smtext):
    """Persist the discovered request as an api_replay recipe so future
    runs can use engines/api_replay_handler.py without re-deriving it."""
    try:
        from utils.request_recipes import save_recipe
    except Exception:
        return
    save_recipe(f"sebi_enforcement_orders_smid_{smid}", {
        "source_id": None,
        "url":     ENDPOINT,
        "method":  "POST",
        "headers": DEFAULT_HEADERS,
        "params":  {},
        "body":    _build_payload(smid, smtext, 0),
        "cookies": {},
        "response_type": "html",
        "extract_strategy": "html_table",
        "pagination": {"param": "nextValue", "start": 0,
                       "limit_param": None, "limit": None,
                       "max_pages": MAX_PAGES_HARDCAP},
        "notes": (f"SEBI Enforcement Orders — smid {smid} ({smtext}). "
                  f"Returns 25-row HTML fragments; iterate nextValue 0..totalpage-1."),
    })


# ---------------------------------------------------------------------------
# Public entry points
# ---------------------------------------------------------------------------
def run_smid(smid, smtext, list_name, csv_filename, max_pages=None,
             session=None):
    """Scrape one sub-list end-to-end; return records + write CSV."""
    own_session = False
    if session is None:
        session = _build_session()
        own_session = True
    scraped_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"[SEBI] starting smid={smid} ({smtext})")
    records = _scrape_smid(session, smid, smtext, list_name, scraped_at,
                           max_pages=max_pages or MAX_PAGES_HARDCAP)
    out_path = os.path.join(DATA_DIR, csv_filename)
    _save_csv(records, out_path)
    _save_recipe(smid, smtext)
    if own_session:
        session.close()
    return records


def run_ssid(ssid, sstext, list_name, csv_filename, max_pages=None,
             session=None):
    """Scrape an Enforcement sub-section that lives directly under an
    ssid (no smid sub-categorization). Used for Recovery Proceedings,
    Auction Notices, Unserved Summons/Notices, Orders Not Served."""
    own_session = False
    if session is None:
        session = _build_session()
        own_session = True
    scraped_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"[SEBI] starting ssid={ssid} ({sstext})")
    records = _scrape_smid(session, smid=0, smtext="", list_name=list_name,
                           scraped_at=scraped_at,
                           max_pages=max_pages or MAX_PAGES_HARDCAP,
                           ssid=str(ssid), sstext=sstext)
    out_path = os.path.join(DATA_DIR, csv_filename)
    _save_csv(records, out_path)
    # Save recipe.
    try:
        from utils.request_recipes import save_recipe
        save_recipe(f"sebi_enforcement_ssid_{ssid}", {
            "source_id": None,
            "url":     ENDPOINT,
            "method":  "POST",
            "headers": DEFAULT_HEADERS,
            "params":  {},
            "body":    _build_payload(0, "", 0, ssid=str(ssid), sstext=sstext),
            "cookies": {},
            "response_type": "html",
            "extract_strategy": "html_table",
            "pagination": {"param": "nextValue", "start": 0,
                           "max_pages": MAX_PAGES_HARDCAP},
            "notes": (f"SEBI Enforcement — ssid {ssid} ({sstext}). "
                      f"smid=0; iterate nextValue 0..totalpage-1."),
        })
    except Exception:
        pass
    if own_session:
        session.close()
    return records


def run(max_pages_per_smid=None, smids=None):
    """Scrape every sub-list in SUB_LISTS, dedupe across smids by url,
    write per-sub-list CSVs and a combined CSV.

    `smids` (optional): list of integer smid values to restrict the run
    (handy for testing or partial re-scrapes). When None, all 8 are run.
    """
    print("=" * 60)
    print("SEBI Enforcement Orders scraper")
    print("=" * 60)
    os.makedirs(DATA_DIR, exist_ok=True)
    session = _build_session()

    all_records = []
    seen_global = set()
    summary = []

    targets = SUB_LISTS
    if smids is not None:
        wanted = set(int(s) for s in smids)
        targets = [t for t in SUB_LISTS if t[0] in wanted]

    for smid, smtext, list_name, csv_filename, _sid in targets:
        recs = run_smid(smid, smtext, list_name, csv_filename,
                        max_pages=max_pages_per_smid, session=session)
        # Cross-smid dedup for the combined CSV (some orders are listed
        # under multiple sub-lists).
        new = 0
        for r in recs:
            key = (r["document_url"] or "").lower() or \
                  (r["name"].lower(), r["details"].split("|", 1)[0])
            if key in seen_global:
                continue
            seen_global.add(key)
            all_records.append(r)
            new += 1
        summary.append((smid, list_name, len(recs), new))

    print(f"\n--- Combined ({len(all_records)} unique records) ---")
    _save_csv(all_records, COMBINED_OUT)
    session.close()

    print("\nPer-sub-list summary:")
    print(f"{'smid':>5}  {'sub_list':<35} {'fetched':>9}  {'new':>6}")
    for smid, list_name, n, new in summary:
        print(f"  {smid:>3}  {list_name:<35} {n:>9}  {new:>6}")
    print(f"  TOTAL combined unique: {len(all_records)}")
    return all_records


if __name__ == "__main__":
    # Quick CLI args:
    #   python -m scrapers.sebi_enforcement_orders                 # all 8 smids, all pages
    #   python -m scrapers.sebi_enforcement_orders --pages 5       # cap pages per smid
    #   python -m scrapers.sebi_enforcement_orders --smid 2 3 6    # subset
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--pages", type=int, default=None,
                    help="max pages per smid (default: until totalpage)")
    ap.add_argument("--smid", type=int, nargs="*", default=None,
                    help="subset of smids to scrape (default: all 8)")
    args = ap.parse_args()
    run(max_pages_per_smid=args.pages, smids=args.smid)
