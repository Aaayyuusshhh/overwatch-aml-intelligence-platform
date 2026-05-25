"""
RBI Overseas Direct Investment (ODI) scraper.

Source: https://rbi.org.in/Scripts/Data_Overseas_Investment.aspx

Each month RBI publishes a press release with an Excel file listing every
Indian party that made an overseas investment (Equity / Loan / Guarantee in
USD million), the JV/WOS name, country, and major activity.

The listing page uses an ASP.NET postback for year filters - we replay it
with the captured __VIEWSTATE / __EVENTVALIDATION fields. Each year returns
~12 press releases (prid). Each press release page links to a single .xlsx
(2018+) or .xls (2017) file on rbidocs.rbi.org.in.

Excel column layout (same across all years):
    A=Sr  B=IndianParty  C=JV/WOS_name  D=JV_or_WOS  E=Country  F=(empty)
    G=Activity  H=Equity  I=Loan  J=Guarantee  K=Total

This is investment data, NOT a sanctions/watchlist. It goes into its own
table: rbi_odi_investments (not watchlist_records).

Outputs:
    data/rbi_odi/                          - cached Excel files
    data/rbi_odi_investments_master.csv    - combined dataset
"""

import csv
import json
import os
import re
import time
from datetime import datetime

import openpyxl
import requests
import xlrd
from bs4 import BeautifulSoup

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CACHE_DIR = os.path.join(PROJECT_ROOT, "data", "rbi_odi")
MASTER_CSV = os.path.join(PROJECT_ROOT, "data", "rbi_odi_investments_master.csv")
PRID_MANIFEST = os.path.join(CACHE_DIR, "_prid_manifest.json")

LISTING_URL = "https://rbi.org.in/Scripts/Data_Overseas_Investment.aspx"
PRESS_URL = "https://rbi.org.in/Scripts/BS_PressReleaseDisplay.aspx?prid={prid}"

YEARS = [str(y) for y in range(2026, 2010, -1)]  # 2017..2026

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}

MASTER_FIELDS = [
    "prid", "press_release_url", "excel_url", "excel_filename",
    "period", "period_from", "period_to",
    "sr_no", "indian_party", "jv_wos_name", "jv_or_wos",
    "country", "activity",
    "equity_usd_mn", "loan_usd_mn", "guarantee_usd_mn", "total_usd_mn",
    "scraped_at",
]


def make_session():
    s = requests.Session()
    s.headers.update(HEADERS)
    # Warm up - cookies from rbi.org.in cover rbidocs.rbi.org.in downloads
    s.get("https://rbi.org.in/", timeout=30)
    return s


def get_listing_prids(session, year):
    """Return list of (prid, link_text) for the given year (POST the ASP.NET form)."""
    r = session.get(LISTING_URL, timeout=30)
    r.raise_for_status()
    soup = BeautifulSoup(r.text, "html.parser")

    form_data = {}
    for inp in soup.find_all("input", type="hidden"):
        name = inp.get("name")
        if name:
            form_data[name] = inp.get("value", "")
    form_data["hdnYear"] = year
    form_data["UsrFontCntr$btn"] = ""

    r2 = session.post(LISTING_URL, data=form_data, timeout=30)
    r2.raise_for_status()
    soup2 = BeautifulSoup(r2.text, "html.parser")

    out = []
    seen = set()
    for a in soup2.find_all("a", href=True):
        m = re.search(r"prid=(\d+)", a["href"])
        if not m:
            continue
        text = a.get_text(strip=True)
        if "overseas direct investment" not in text.lower():
            continue
        prid = m.group(1)
        if prid in seen:
            continue
        seen.add(prid)
        out.append((prid, text))
    return out


def get_excel_url(session, prid):
    """Fetch press release page; return Excel URL (or None)."""
    url = PRESS_URL.format(prid=prid)
    r = session.get(url, timeout=30)
    if r.status_code != 200:
        return None
    soup = BeautifulSoup(r.text, "html.parser")

    # Look for any link whose href ends with .xlsx or .xls
    candidates = []
    for a in soup.find_all("a", href=True):
        href = a["href"]
        text = a.get_text(strip=True).lower()
        href_lower = href.lower()
        if href_lower.endswith(".xlsx") or href_lower.endswith(".xls"):
            candidates.append((text, href))

    # Prefer link whose text mentions overseas / OFDI / outward
    for text, href in candidates:
        if any(k in text for k in ("overseas", "ofdi", "outward")):
            return _abs(href)
    if candidates:
        return _abs(candidates[0][1])
    return None


def _abs(href):
    if href.startswith("http://"):
        return "https://" + href[len("http://"):]
    if href.startswith("https://"):
        return href
    return "https://rbidocs.rbi.org.in" + (href if href.startswith("/") else "/" + href)


def download_excel(session, url, prid):
    """Download Excel; return local path or None."""
    fname = url.rsplit("/", 1)[-1].split("?")[0]
    if not fname.lower().endswith((".xls", ".xlsx")):
        fname = f"prid{prid}.xlsx"
    path = os.path.join(CACHE_DIR, fname)
    if os.path.exists(path) and os.path.getsize(path) > 2000:
        return path

    try:
        r = session.get(
            url, timeout=120,
            headers={**HEADERS, "Referer": PRESS_URL.format(prid=prid)},
            allow_redirects=True,
        )
    except requests.RequestException as e:
        print(f"  ! download error for prid={prid}: {e}")
        return None

    if r.status_code != 200 or len(r.content) < 2000:
        print(f"  ! bad download prid={prid} status={r.status_code} bytes={len(r.content)}")
        return None
    # Reject HTML (bot wall)
    head = r.content[:80].lower()
    if b"<html" in head or b"<!doctype" in head:
        print(f"  ! got HTML instead of Excel for prid={prid} ({len(r.content)} bytes)")
        return None
    with open(path, "wb") as f:
        f.write(r.content)
    return path


# --- Parsing -----------------------------------------------------------------

def _safe_float(v):
    import math as _m
    if v is None or v == "":
        return 0.0
    try:
        f = float(v)
    except (TypeError, ValueError):
        s = str(v).replace(",", "").strip()
        try:
            f = float(s)
        except ValueError:
            return 0.0
    if _m.isnan(f) or _m.isinf(f):
        return 0.0
    return f


def _safe_int(v):
    if v is None or v == "":
        return None
    try:
        return int(float(v))
    except (TypeError, ValueError):
        return None


def _clean(s):
    if s is None:
        return ""
    return str(s).replace("\xa0", " ").strip()


def _parse_period(text):
    """Extract (period_from, period_to). Handles both 'From : DD/MM/YYYY ... To : DD/MM/YYYY'
    (modern files) and 'FROM DD/MM/YYYY TO DD/MM/YYYY' (older cumulative files, no colons)."""
    m = re.search(
        r"From\s*:?\s*(\d{2}/\d{2}/\d{4}).*?To\s*:?\s*(\d{2}/\d{2}/\d{4})",
        text, re.IGNORECASE | re.DOTALL,
    )
    if m:
        return m.group(1), m.group(2)
    return None, None


def _rows_from_sheet(rows_iter):
    """
    Given iterable of row-tuples, find the period and header row, then yield
    parsed data dicts.

    Layout (all years 2017-2026):
        col 0 = Sr.
        col 1 = Indian Party
        col 2 = JV/WOS name
        col 3 = JV or WOS
        col 4 = Country
        col 5 = (empty / merged with country)
        col 6 = Activity
        col 7 = Equity (USD mn)
        col 8 = Loan
        col 9 = Guarantee Issued
        col 10 = Total
    """
    all_rows = list(rows_iter)
    period_from = period_to = None
    for row in all_rows[:20]:
        for cell in row:
            if cell is None:
                continue
            s = str(cell)
            if "from" in s.lower() and "to" in s.lower():
                pf, pt = _parse_period(s)
                if pf:
                    period_from, period_to = pf, pt
                    break
        if period_from:
            break

    # Find header row containing "Indian Party"
    header_idx = None
    for i, row in enumerate(all_rows):
        for cell in row:
            if cell and "indian party" in str(cell).lower():
                header_idx = i
                break
        if header_idx is not None:
            break
    if header_idx is None:
        return

    data_start = header_idx + 1
    # Skip sub-header row (contains "Equity")
    if data_start < len(all_rows):
        if any(c and "equity" in str(c).lower() for c in all_rows[data_start] if c is not None):
            data_start += 1
    # Skip blank rows
    while data_start < len(all_rows):
        r = all_rows[data_start]
        if r and r[0] is not None and str(r[0]).strip():
            break
        data_start += 1

    for row in all_rows[data_start:]:
        if not row or len(row) < 7:
            continue
        sr = row[0]
        if sr is None or str(sr).strip() == "":
            continue
        sr_int = _safe_int(sr)
        if sr_int is None:
            # Hit a non-numeric (likely "Total" / footnote) - stop
            label = _clean(sr).lower()
            if "total" in label or "note" in label or "*" in label:
                break
            continue

        indian_party = _clean(row[1] if len(row) > 1 else "")
        if not indian_party or len(indian_party) < 2:
            continue

        jv_wos_name = _clean(row[2] if len(row) > 2 else "")
        jv_or_wos = _clean(row[3] if len(row) > 3 else "")
        country = _clean(row[4] if len(row) > 4 else "")
        activity = _clean(row[6] if len(row) > 6 else "")
        equity = _safe_float(row[7] if len(row) > 7 else None)
        loan = _safe_float(row[8] if len(row) > 8 else None)
        guarantee = _safe_float(row[9] if len(row) > 9 else None)
        total = _safe_float(row[10] if len(row) > 10 else None)

        yield {
            "period_from": period_from,
            "period_to": period_to,
            "sr_no": sr_int,
            "indian_party": indian_party,
            "jv_wos_name": jv_wos_name,
            "jv_or_wos": jv_or_wos,
            "country": country,
            "activity": activity,
            "equity_usd_mn": equity,
            "loan_usd_mn": loan,
            "guarantee_usd_mn": guarantee,
            "total_usd_mn": total,
        }


def parse_excel(path):
    """Parse .xlsx (openpyxl) or .xls (xlrd). Return (first_period_label, rows).
    Each row carries its OWN sheet name in the 'period' key, so cumulative
    historical files (multi-sheet) are handled correctly."""
    out = []
    first_label = None
    is_xls = path.lower().endswith(".xls") and not path.lower().endswith(".xlsx")
    try:
        if is_xls:
            wb = xlrd.open_workbook(path)
            for sname in wb.sheet_names():
                if "summary" in sname.lower():
                    continue
                ws = wb.sheet_by_name(sname)
                rows = (
                    tuple(ws.cell_value(i, j) for j in range(ws.ncols))
                    for i in range(ws.nrows)
                )
                if first_label is None:
                    first_label = sname
                for r in _rows_from_sheet(rows):
                    r["period"] = sname
                    out.append(r)
        else:
            wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
            try:
                for sname in wb.sheetnames:
                    if "summary" in sname.lower():
                        continue
                    ws = wb[sname]
                    if first_label is None:
                        first_label = sname
                    for r in _rows_from_sheet(ws.iter_rows(values_only=True)):
                        r["period"] = sname
                        out.append(r)
            finally:
                wb.close()
    except Exception as e:
        print(f"  ! parse error {os.path.basename(path)}: {e}")
        return None, []
    return first_label, out


# --- Orchestration -----------------------------------------------------------

def build_manifest(session, force=False):
    """Collect all (prid, year, title) tuples by paging through the year filter."""
    if os.path.exists(PRID_MANIFEST) and not force:
        with open(PRID_MANIFEST) as f:
            return json.load(f)

    manifest = []
    seen = set()
    for year in YEARS:
        try:
            items = get_listing_prids(session, year)
        except Exception as e:
            print(f"  ! year {year} listing failed: {e}")
            continue
        new = 0
        for prid, text in items:
            if prid in seen:
                continue
            seen.add(prid)
            manifest.append({"prid": prid, "year": year, "title": text})
            new += 1
        print(f"  year {year}: {len(items)} links, {new} new (total {len(manifest)})")
        time.sleep(1)

    with open(PRID_MANIFEST, "w") as f:
        json.dump(manifest, f, indent=2)
    return manifest


def main():
    os.makedirs(CACHE_DIR, exist_ok=True)
    session = make_session()

    print("[1/4] Collecting press release manifest across years 2017-2026...")
    manifest = build_manifest(session, force=True)
    print(f"  -> {len(manifest)} ODI press releases\n")

    print("[2/4] Resolving Excel URLs and downloading...")
    excel_records = []  # list of (entry, excel_url, local_path)
    for i, entry in enumerate(manifest, 1):
        prid = entry["prid"]
        excel_url = get_excel_url(session, prid)
        if not excel_url:
            print(f"  [{i}/{len(manifest)}] prid={prid}: no excel link found - skip")
            time.sleep(0.5)
            continue
        path = download_excel(session, excel_url, prid)
        if not path:
            time.sleep(0.5)
            continue
        excel_records.append((entry, excel_url, path))
        if i % 10 == 0 or i == len(manifest):
            print(f"  [{i}/{len(manifest)}] prid={prid} -> {os.path.basename(path)}")
        time.sleep(1)
    print(f"  -> {len(excel_records)} Excel files cached\n")

    print("[3/4] Parsing all Excel files...")
    all_rows = []
    scraped_at = datetime.utcnow().isoformat(timespec="seconds") + "Z"
    for entry, excel_url, path in excel_records:
        first_label, rows = parse_excel(path)
        for r in rows:
            r["prid"] = entry["prid"]
            r["press_release_url"] = PRESS_URL.format(prid=entry["prid"])
            r["excel_url"] = excel_url
            r["excel_filename"] = os.path.basename(path)
            # parse_excel already set r["period"] to the sheet's own name.
            # Only fall back to the press release title if parsing produced no period.
            if not r.get("period"):
                r["period"] = first_label or entry["title"]
            r["scraped_at"] = scraped_at
            all_rows.append(r)
        if len(rows) == 0:
            print(f"  ! 0 rows from {os.path.basename(path)} (prid={entry['prid']})")
    print(f"  -> {len(all_rows):,} total investment records\n")

    print("[4/4] Writing master CSV...")
    with open(MASTER_CSV, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=MASTER_FIELDS, extrasaction="ignore")
        w.writeheader()
        for r in all_rows:
            w.writerow(r)
    print(f"  -> {MASTER_CSV} ({len(all_rows):,} rows)")

    # Summary
    if all_rows:
        parties = set(r["indian_party"] for r in all_rows)
        countries = set(r["country"] for r in all_rows if r["country"])
        total_usd = sum(r["total_usd_mn"] for r in all_rows)
        print()
        print(f"  Unique Indian parties: {len(parties):,}")
        print(f"  Unique countries:      {len(countries)}")
        print(f"  Total USD mn:          {total_usd:,.2f}")


if __name__ == "__main__":
    main()
