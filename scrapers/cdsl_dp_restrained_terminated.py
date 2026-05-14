"""
CDSL — DP Restrained & DPs Terminated (bonus, non-canonical).

Source: https://www.cdslindia.com/Investors/Regulatory-Orders.aspx

Same ASP.NET WebForm pattern as #161 (Arbitration Awards): a single
`FrYearDropDownList` covers FY 2007-2008 through 2026-2027. Each
year, the page exposes two .xls/.xlsx download links:

  DP_Restrained_during_YYYY-YYYY.xls
  List of Terminated DPs-YYYY-YY.xlsx       (note the short year form)

Plus a constant historical compilation:
  Action taken on DP pursuant to Inspection from 01042018 onwards.xlsx

The download endpoint is /Common/DownLoadFile.aspx with
action=circular&filename=<file>. We walk all 20 years via postback,
collect every distinct download URL, fetch each, parse with pandas,
and emit one record per DP per year.

This is recorded under source_list = "DP Restrained and Terminated"
to keep the two streams jointly searchable; details carry the per-row
type (Restrained / Terminated / Action) and the FY.
"""

import csv
import io
import os
import re
import time
from datetime import datetime
from urllib.parse import urljoin

import pandas as pd
import requests
from bs4 import BeautifulSoup
import warnings
warnings.filterwarnings("ignore")

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LIST_URL = "https://www.cdslindia.com/Investors/Regulatory-Orders.aspx"
OUTPUT_FILE = os.path.join(PROJECT_ROOT, "data",
                            "cdsl_dp_restrained_terminated.csv")

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
      "Accept": "text/html,*/*;q=0.8",
      "Referer": LIST_URL}

YEARS = [f"{y}-{y+1}" for y in range(2007, 2027)]


def _clean(s):
    if s is None:
        return ""
    s = str(s)
    if s.lower() in ("nan", "nat", "none"):
        return ""
    return re.sub(r"\s+", " ", s.replace("\xa0", " ")).strip(" .,;-")


def _state(html):
    out = {}
    soup = BeautifulSoup(html, "html.parser")
    for name in ("__VIEWSTATE", "__VIEWSTATEGENERATOR",
                 "__EVENTVALIDATION", "__VIEWSTATEENCRYPTED"):
        el = soup.find("input", attrs={"name": name})
        if el and el.get("value") is not None:
            out[name] = el["value"]
    return out


def _collect_xls_links(html):
    """Return list of absolute URLs for every .xls/.xlsx href on the
    page response (typically two per FY: Restrained + Terminated)."""
    hrefs = re.findall(r'href=["\']([^"\']+\.xlsx?[^"\']*)["\']', html, re.I)
    return [urljoin(LIST_URL, h) for h in hrefs]


def _infer_kind(url):
    u = url.lower()
    if "restrained" in u:
        return "Restrained"
    if "terminated" in u:
        return "Terminated"
    if "action taken" in u or "pursuant_to_inspection" in u:
        return "Action Taken (Inspection)"
    return "Other"


def _parse_xls(content, source_url, kind, fy, scraped_at):
    """Parse a single workbook into records. The two file types share
    different schemas — both have a DP-name column plus action details.
    We pick the longest text column as the name field; everything else
    goes in details."""
    def _read(header):
        bio = io.BytesIO(content)
        for eng in ("openpyxl", "xlrd"):
            try:
                return pd.read_excel(bio, engine=eng, header=header)
            except Exception:
                bio.seek(0)
        return None

    df = _read(0)
    if df is None or df.empty:
        return []
    df.columns = [str(c).strip() for c in df.columns]
    # Some CDSL files have a merged "title" cell at row 0 that pandas reads as
    # the header, leaving subsequent columns as "Unnamed: N". Detect and
    # re-read with header=1 so we get real column names like "DP Name".
    unnamed = sum(1 for c in df.columns if c.lower().startswith("unnamed"))
    if unnamed >= max(2, len(df.columns) - 1):
        df2 = _read(1)
        if df2 is not None and not df2.empty:
            df = df2
            df.columns = [str(c).strip() for c in df.columns]
    # Try to find a "name" column heuristically.
    name_col = None
    for c in df.columns:
        cl = c.lower()
        if cl.startswith("unnamed"):
            continue
        if any(k in cl for k in ("name of dp", "dp name", "name of depository",
                                  "depository participant", "name", "participant")):
            name_col = c
            break
    if name_col is None:
        # Skip files where we can't pick a name column.
        return []
    rows = []
    for _, r in df.iterrows():
        nm = _clean(r[name_col])
        if not nm or nm.lower() == name_col.lower():
            continue
        # Extract other useful columns
        detail_bits = [f"Type: {kind}", f"FY: {fy}"]
        for c in df.columns:
            if c == name_col:
                continue
            v = _clean(r[c])
            if v:
                detail_bits.append(f"{c}: {v[:200]}")
        rows.append({
            "source_agency": "Central Depository Services (India) Limited (CDSL)",
            "source_list":   "DP Restrained and Terminated",
            "case_unit":     "",
            "name":          nm,
            "father_name":   "",
            "date_of_birth": "",
            "gender":        "",
            "address":       "",
            "reward_amount": "",
            "details":       " | ".join(detail_bits),
            "has_document":  "Yes",
            "document_url":  source_url,
            "detail_page_url": LIST_URL,
            "interpol_notice_id": "",
            "link_kind":     "manual_discovery",
            "scraped_at":    scraped_at,
            "enrichment_status": "",
        })
    return rows


def scrape():
    sess = requests.Session()
    sess.headers.update(UA)
    sess.verify = False

    r = sess.get(LIST_URL, timeout=45)
    if r.status_code != 200:
        raise RuntimeError(f"CDSL DP: initial GET {r.status_code}")
    state = _state(r.text)
    scraped_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    # Walk every year + capture every xls/xlsx URL that appears.
    xls_urls = {}        # url -> fy associated when first seen
    # the landing already corresponds to one year, capture it too.
    for u in _collect_xls_links(r.text):
        xls_urls.setdefault(u, YEARS[-1])  # tentatively the default-selected fy

    for fy in YEARS[::-1]:
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
        state = _state(rr.text)
        new_urls = 0
        for u in _collect_xls_links(rr.text):
            if u not in xls_urls:
                xls_urls[u] = fy
                new_urls += 1
        print(f"  {fy}: links seen={new_urls}")
        time.sleep(0.8)
    print(f"  TOTAL distinct .xls/.xlsx URLs: {len(xls_urls)}")

    # Fetch + parse each workbook.
    all_rows = []
    for u, fy in xls_urls.items():
        kind = _infer_kind(u)
        # Better-infer the FY from the filename when we can.
        m = re.search(r'(\d{4}[-_]\d{2,4})', u)
        fy_from_name = m.group(1).replace("_", "-") if m else fy
        try:
            f = sess.get(u, timeout=45)
        except Exception as e:
            print(f"  download failed: {u[:90]}  err={type(e).__name__}")
            continue
        if f.status_code != 200 or len(f.content) < 200:
            print(f"  download bad {f.status_code} {len(f.content)} {u[:90]}")
            continue
        try:
            recs = _parse_xls(f.content, u, kind, fy_from_name, scraped_at)
        except Exception as e:
            print(f"  parse failed for {u[:80]}: {type(e).__name__}: {e}")
            continue
        if recs:
            print(f"  parsed {len(recs):>4} | {kind:<25} FY {fy_from_name} | {u[u.rfind('/')+1:][:55]}")
        all_rows.extend(recs)
    return all_rows


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
    print("CDSL DP Restrained & Terminated (bonus)")
    print("=" * 60)
    rows = scrape()
    if not rows:
        raise RuntimeError("CDSL DP: 0 rows parsed")
    save_to_csv(rows, OUTPUT_FILE)
    return rows


if __name__ == "__main__":
    run()
