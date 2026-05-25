#!/usr/bin/env python3
"""Recon all 7 MCA RD/ROC pages with a real-browser session."""
from __future__ import annotations
import re, time, warnings, urllib3
import requests
from bs4 import BeautifulSoup

warnings.filterwarnings("ignore")
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

URLS = [
    ("disqualified_directors", "https://www.mca.gov.in/content/mca/global/en/data-and-reports/rd-roc-info/disqualified-directors.html"),
    ("directors_struck_off",   "https://www.mca.gov.in/content/mca/global/en/data-and-reports/rd-roc-info/directors-struck-companies.html"),
    ("proclaimed_offenders",   "https://www.mca.gov.in/content/mca/global/en/data-and-reports/rd-roc-info/proclaimed-offenders.html"),
    ("companies_struck_off",   "https://www.mca.gov.in/content/mca/global/en/data-and-reports/rd-roc-info/companies-struck-roc.html"),
    ("notice_strike_off",      "https://www.mca.gov.in/content/mca/global/en/data-and-reports/rd-roc-info/notice-strike-off.html"),
    ("roc_adjudication",       "https://www.mca.gov.in/content/mca/global/en/data-and-reports/rd-roc-info/roc-adjudication-orders.html"),
    ("rd_compounding",         "https://www.mca.gov.in/content/mca/global/en/data-and-reports/rd-roc-info/rd-compounding-orders.html"),
]
H = {
    "User-Agent": ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"),
    "Accept": ("text/html,application/xhtml+xml,application/xml;q=0.9,"
               "image/avif,image/webp,image/apng,*/*;q=0.8,"
               "application/signed-exchange;v=b3;q=0.7"),
    "Accept-Language": "en-IN,en-US;q=0.9,en;q=0.8",
    "Accept-Encoding": "gzip, deflate, br",
    "Sec-Ch-Ua": '"Chromium";v="125", "Not_A Brand";v="24"',
    "Sec-Ch-Ua-Mobile": "?0",
    "Sec-Ch-Ua-Platform": '"Linux"',
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
    "Sec-Fetch-User": "?1",
    "Upgrade-Insecure-Requests": "1",
}


def main():
    s = requests.Session()
    # Establish session by hitting homepage
    s.get("https://www.mca.gov.in/", headers=H, timeout=20, verify=False)

    print(f"{'label':25s} {'status':6s} {'bytes':>8s} {'ms':>6s} "
          f"{'pdfs':>5s} {'total':>7s} {'sel_opts':>8s} title / error")
    for label, url in URLS:
        t0 = time.time()
        try:
            r = s.get(url, headers=H, timeout=30, verify=False)
            ms = int((time.time()-t0)*1000)
            soup = BeautifulSoup(r.text, "html.parser")
            title = (soup.title.string or "").strip()[:60] if soup.title else "-"
            pdfs = len([a for a in soup.find_all("a", href=True)
                       if ".pdf" in a["href"].lower()])
            text = soup.get_text(" ", strip=True)
            m = re.search(r"Showing Results\s+\d+-\d+\s*of\s*(\d+)", text)
            total = m.group(1) if m else "?"
            sel_opts = 0
            for sel in soup.find_all("select"):
                opts = sel.find_all("option")
                if len(opts) > 5:
                    sel_opts = max(sel_opts, len(opts))
            print(f"{label:25s} {str(r.status_code):6s} {len(r.content):>8d} "
                  f"{ms:>6d} {pdfs:>5d} {total:>7s} {sel_opts:>8d}  {title}")
        except Exception as e:
            print(f"{label:25s} ERROR  {type(e).__name__}: {str(e)[:60]}")
        time.sleep(0.5)


if __name__ == "__main__":
    main()
