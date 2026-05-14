"""
CDSL Arbitration Awards (#161).

Source: https://www.cdslindia.com/Investors/arbitration-awards.aspx

The page is an ASP.NET WebForm. A "FrYearDropDownList" select carries
20 financial-year options (2007-2008 through 2026-2027); changing the
year triggers `__doPostBack('FrYearDropDownList','')` which re-renders
the table below with that year's awards.

We drive the postback by:
  1. GET the page to grab __VIEWSTATE / __VIEWSTATEGENERATOR /
     __EVENTVALIDATION (always required for ASP.NET).
  2. POST back with FrYearDropDownList=<FY> and __EVENTTARGET=<dropdown>,
     using the session cookie set by step 1.
  3. Parse the rendered <table> on each response.

Table columns:
  Award Date | Claimant Name | Respondent Name | Details

Names are appended with a trailing "~~~~" obfuscation marker in the
HTML — stripped before saving.
"""

import csv
import os
import re
import time
from datetime import datetime
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup
import warnings
warnings.filterwarnings("ignore")

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LIST_URL = "https://www.cdslindia.com/Investors/arbitration-awards.aspx"
OUTPUT_FILE = os.path.join(PROJECT_ROOT, "data",
                            "cdsl_arbitration_awards_161.csv")

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
      "Accept": "text/html,application/xhtml+xml,*/*;q=0.8",
      "Referer": LIST_URL}

# Financial year options the dropdown lists. 20 of them.
YEARS = [f"{y}-{y+1}" for y in range(2007, 2027)]   # 2007-2008 ... 2026-2027

TILDE_RE = re.compile(r"~+$")
NUM_RE = re.compile(r"^\s*\d+\s*$")


def _clean(s):
    if s is None:
        return ""
    s = str(s).replace("\xa0", " ")
    s = TILDE_RE.sub("", s)             # strip trailing ~~~~ obfuscation
    return re.sub(r"\s+", " ", s).strip(" .,;-")


def _extract_state(html):
    """Return dict of hidden ASP.NET fields needed for a postback."""
    soup = BeautifulSoup(html, "html.parser")
    state = {}
    for name in ("__VIEWSTATE", "__VIEWSTATEGENERATOR",
                 "__EVENTVALIDATION", "__VIEWSTATEENCRYPTED"):
        el = soup.find("input", attrs={"name": name})
        if el and el.get("value") is not None:
            state[name] = el["value"]
    return state


def _parse_table(html, fy):
    """Return list of records from the post-render HTML."""
    soup = BeautifulSoup(html, "html.parser")
    rows = []
    for tr in soup.select("table tr"):
        cells = [_clean(c.get_text(" ", strip=True))
                 for c in tr.find_all(["td", "th"])]
        if len(cells) < 4:
            continue
        date_, claim, respondent, _det = cells[:4]
        # Skip header / sr-no rows
        if claim.lower() == "claimant name":
            continue
        if NUM_RE.match(claim):           # numeric placeholder, not a name
            continue
        if not claim:
            continue
        rows.append((date_, claim, respondent, fy))
    return rows


def scrape():
    sess = requests.Session()
    sess.headers.update(UA)
    sess.verify = False

    # Initial GET to seed cookies + ASP.NET state.
    r = sess.get(LIST_URL, timeout=45)
    if r.status_code != 200:
        raise RuntimeError(f"CDSL Arbitration: initial GET {r.status_code}")
    state = _extract_state(r.text)
    if "__VIEWSTATE" not in state:
        raise RuntimeError("CDSL Arbitration: no __VIEWSTATE on landing page")

    scraped_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    out = []
    seen = set()

    # Default (current) year is already rendered in r.text. Pre-pull it
    # to avoid one extra round-trip.
    cur_year = YEARS[-1]
    for d, c, r_, fy in _parse_table(r.text, cur_year):
        key = (c.lower(), d, r_.lower())
        if key not in seen:
            seen.add(key)
            out.append((d, c, r_, fy))
    print(f"  {cur_year}: {len(out)} rows (from landing)")

    # Iterate the remaining years via postback.
    for fy in YEARS[::-1]:                # newest -> oldest
        if fy == cur_year:
            continue
        data = {
            "__EVENTTARGET":           "FrYearDropDownList",
            "__EVENTARGUMENT":         "",
            "__LASTFOCUS":             "",
            "__VIEWSTATE":             state["__VIEWSTATE"],
            "__VIEWSTATEGENERATOR":    state.get("__VIEWSTATEGENERATOR", ""),
            "__EVENTVALIDATION":       state.get("__EVENTVALIDATION", ""),
            "FrYearDropDownList":      fy,
        }
        try:
            rr = sess.post(LIST_URL, data=data, timeout=45)
        except Exception as e:
            print(f"  {fy}: POST failed: {type(e).__name__}: {e}")
            continue
        if rr.status_code != 200:
            print(f"  {fy}: status {rr.status_code}")
            continue
        # ASP.NET regenerates state on each response; refresh it.
        state = _extract_state(rr.text)
        rows = _parse_table(rr.text, fy)
        added = 0
        for d, c, r_, _fy in rows:
            key = (c.lower(), d, r_.lower())
            if key in seen:
                continue
            seen.add(key)
            out.append((d, c, r_, fy))
            added += 1
        print(f"  {fy}: {len(rows)} rows ({added} new after dedup)")
        time.sleep(1.0)

    print(f"  TOTAL unique awards across all years: {len(out)}")
    return [{
        "source_agency": "Central Depository Services (India) Limited (CDSL)",
        "source_list":   "Arbitration Awards",
        "case_unit":     "",
        "name":          claim,
        "father_name":   "",
        "date_of_birth": "",
        "gender":        "",
        "address":       "",
        "reward_amount": "",
        "details":       (f"Respondent: {respondent} | Award Date: {date_} "
                          f"| FY: {fy}"),
        "has_document":  "No",
        "document_url":  "",
        "detail_page_url": LIST_URL,
        "interpol_notice_id": "",
        "link_kind":     "constructed",
        "scraped_at":    scraped_at,
        "enrichment_status": "",
    } for date_, claim, respondent, fy in out]


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
    print("CDSL Arbitration Awards (#161)")
    print("=" * 60)
    rows = scrape()
    if not rows:
        raise RuntimeError("CDSL Arbitration: 0 rows parsed")
    save_to_csv(rows, OUTPUT_FILE)
    return rows


if __name__ == "__main__":
    run()
