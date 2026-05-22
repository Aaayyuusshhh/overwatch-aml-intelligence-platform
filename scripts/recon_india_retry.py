#!/usr/bin/env python3
"""Batch-probe blocked India sources to see which are now reachable.
Prints a one-row-per-URL summary: status, bytes, cloudflare/JS signals, table count."""
from __future__ import annotations
import re, time, warnings, urllib3, sys
from concurrent.futures import ThreadPoolExecutor, as_completed
import requests
from bs4 import BeautifulSoup

warnings.filterwarnings("ignore")
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

URLS = [
    ("nse_circulars",      "https://www.nseindia.com/regulations/listing-compliance/nse-market-regulation"),
    ("nse_actions",        "https://www.nseindia.com/regulations/enforcement-actions-taken"),
    ("ed_enforcement",     "https://enforcementdirectorate.gov.in/press-release"),
    ("rbi_ffmc_cancelled", "https://www.rbi.org.in/scripts/BS_ViewBankwise.aspx"),
    ("ibbi_orders",        "https://ibbi.gov.in/orders"),
    ("sfio_home",          "https://sfio.gov.in/"),
    ("sfio_convictions",   "https://sfio.gov.in/convictions"),
]
H = {"User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/125.0 Safari/537.36"),
     "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
     "Accept-Language": "en-IN,en;q=0.9"}

def probe(label, url):
    out = {"label": label, "url": url, "status": "-", "bytes": 0,
           "ms": 0, "tables": 0, "links": 0, "cloudflare": False,
           "js_only": False, "title": "", "error": ""}
    t0 = time.time()
    try:
        r = requests.get(url, headers=H, timeout=20, verify=False, allow_redirects=True)
        out["ms"] = int((time.time()-t0)*1000)
        out["status"] = r.status_code
        out["bytes"] = len(r.content)
        text = r.text
        if "cloudflare" in text.lower() or "cf-ray" in r.headers.get("server","").lower() \
                or "challenge-platform" in text.lower():
            out["cloudflare"] = True
        soup = BeautifulSoup(text, "html.parser")
        if soup.title and soup.title.string:
            out["title"] = soup.title.string.strip()[:60]
        tables = soup.find_all("table")
        out["tables"] = len(tables)
        out["links"] = len(soup.find_all("a"))
        if out["bytes"] < 5000 and ("loading" in text.lower() or "javascript" in text.lower()):
            out["js_only"] = True
    except Exception as e:
        out["ms"] = int((time.time()-t0)*1000)
        out["error"] = f"{type(e).__name__}: {str(e)[:80]}"
    return out


def main():
    results = []
    with ThreadPoolExecutor(max_workers=4) as ex:
        futs = {ex.submit(probe, lbl, url): lbl for lbl, url in URLS}
        for fut in as_completed(futs, timeout=60):
            results.append(fut.result())
    results.sort(key=lambda r: r["label"])
    print(f"{'label':22s} {'status':6s} {'bytes':>8s} {'ms':>6s} {'tbl':>4s} "
          f"{'links':>5s} {'CF':>3s} title / error")
    for r in results:
        cf = "Y" if r["cloudflare"] else "-"
        title_or_err = r["error"] or r["title"]
        print(f"{r['label']:22s} {str(r['status']):6s} {r['bytes']:>8d} "
              f"{r['ms']:>6d} {r['tables']:>4d} {r['links']:>5d} {cf:>3s} {title_or_err}")


if __name__ == "__main__":
    main()
