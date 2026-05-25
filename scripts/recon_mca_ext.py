#!/usr/bin/env python3
"""Recon for extended MCA scraping:
 - 3 MCA portal pages (defaulter companies/directors, dormant companies)
 - TaxGuru article on corporate frauds / chit fund scams
 - 19 legacy EES PDF URLs from DEFAULTERS.doc

Reports: status, size, PDFs visible, total-results count.
"""
from __future__ import annotations
import re, time, warnings, json
import urllib3
import requests
from bs4 import BeautifulSoup

warnings.filterwarnings("ignore")
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

H_BROWSER = {
    "User-Agent": ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"),
    "Accept": ("text/html,application/xhtml+xml,application/xml;q=0.9,"
               "image/avif,image/webp,image/apng,*/*;q=0.8"),
    "Accept-Language": "en-IN,en-US;q=0.9,en;q=0.8",
    "Accept-Encoding": "gzip, deflate, br",
    "Sec-Ch-Ua": '"Chromium";v="125", "Not_A Brand";v="24"',
    "Sec-Ch-Ua-Mobile": "?0",
    "Sec-Ch-Ua-Platform": '"Linux"',
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
    "Upgrade-Insecure-Requests": "1",
}

MCA_PORTAL_URLS = [
    ("defaulter_companies",   "https://www.mca.gov.in/content/mca/global/en/data-and-reports/company-llp-info/under-alert/defaulter-companies.html"),
    ("defaulter_directors",   "https://www.mca.gov.in/content/mca/global/en/data-and-reports/company-llp-info/under-alert/defaulter-directors.html"),
    ("dormant_companies",     "https://www.mca.gov.in/content/mca/global/en/data-and-reports/company-llp-info/under-alert/dormant-companies.html"),
]

TAXGURU_URL = "https://taxguru.in/corporate-law/list-companies-involved-corporate-fraudschit-fund-scams.html"

EES_BASE = "http://www.mca.gov.in/MCA21/dca/EES_Companies_List/"
EES_PDFS = [
    "CompanyListA_F.pdf", "CompanyListG_L.pdf", "CompanyListM_R.pdf", "CompanyListS_Z.pdf",
    "DIRLIST1_00000000_00120000.pdf", "DIRLIST2_00120000_00300000.pdf",
    "DIRLIST3_00300000_00500000.pdf", "DIRLIST4_00500000_00750000.pdf",
    "DIRLIST5_00750000_01050000.pdf", "DIRLIST6_01050000_01300000.pdf",
    "DIRLIST7_01300000_01600000.pdf", "DIRLIST8_01600000_01750000.pdf",
    "DIRLIST9_01750000_01900000.pdf", "DIRLIST10_01900000_02050000.pdf",
    "DIRLIST11_02050000_02200000.pdf", "DIRLIST12_02200000_02400000.pdf",
    "DIRLIST13_02400000_02700000.pdf", "DIRLIST14_02700000_00400000.pdf",
    "SEC_SINGLE_LIST.pdf",
]


def probe_mca_portal(s):
    print("\n=== MCA PORTAL PAGES ===")
    print(f"{'label':25s} {'status':6s} {'bytes':>8s} {'ms':>6s} {'pdfs':>5s} "
          f"{'total':>7s} folder/title")
    for label, url in MCA_PORTAL_URLS:
        t0 = time.time()
        try:
            r = s.get(url, headers=H_BROWSER, timeout=30, verify=False)
            ms = int((time.time()-t0)*1000)
            soup = BeautifulSoup(r.text, "html.parser")
            title = (soup.title.string or "").strip()[:50] if soup.title else "-"
            pdfs = len([a for a in soup.find_all("a", href=True) if ".pdf" in a["href"].lower()])
            text = soup.get_text(" ", strip=True)
            m = re.search(r"Showing Results\s+\d+-\d+\s*of\s*(\d+)", text)
            total = m.group(1) if m else "?"
            # Look for data-dialog attribute containing folder ID
            folder = "?"
            for tag in soup.find_all(attrs={"data-dialog": True}):
                d = tag["data-dialog"]
                fm = re.search(r'"folder"\s*:\s*"?(\d+)"?', d)
                if fm:
                    folder = fm.group(1)
                    break
            print(f"{label:25s} {str(r.status_code):6s} {len(r.content):>8d} "
                  f"{ms:>6d} {pdfs:>5d} {total:>7s}  folder={folder} | {title}")
        except Exception as e:
            print(f"{label:25s} ERROR  {type(e).__name__}: {str(e)[:60]}")
        time.sleep(0.5)


def probe_taxguru(s):
    print("\n=== TAXGURU ARTICLE ===")
    H = dict(H_BROWSER)
    try:
        r = s.get(TAXGURU_URL, headers=H, timeout=30, verify=False)
        soup = BeautifulSoup(r.text, "html.parser")
        title = (soup.title.string or "").strip()[:60] if soup.title else "-"
        text = soup.get_text(" ", strip=True)
        # Look for PDF or table or numbered list
        tables = soup.find_all("table")
        ol = soup.find_all("ol")
        ul = soup.find_all("ul")
        pdfs = [a["href"] for a in soup.find_all("a", href=True) if ".pdf" in a["href"].lower()]
        print(f"  status={r.status_code} bytes={len(r.content)} title={title}")
        print(f"  tables={len(tables)} ol={len(ol)} ul={len(ul)} pdfs={len(pdfs)}")
        if pdfs:
            for p in pdfs[:5]:
                print(f"    PDF: {p}")
        # Print some context around 145 companies
        for kw in ("145", "Annexure", "Annexure I"):
            i = text.find(kw)
            if i > 0:
                print(f"  ctx '{kw}': ...{text[max(0,i-40):i+150]}...")
                break
    except Exception as e:
        print(f"  ERROR: {type(e).__name__}: {e}")


def probe_ees():
    print("\n=== EES LEGACY PDFS (2011 URLs) ===")
    s = requests.Session()
    s.headers.update(H_BROWSER)
    ok_count = 0
    print(f"{'pdf':45s} {'status':6s} {'bytes':>10s} valid_pdf")
    for name in EES_PDFS:
        url = EES_BASE + name
        try:
            r = s.get(url, timeout=30, verify=False, allow_redirects=True, stream=True)
            content = r.content[:8] if r.content else b""
            is_pdf = content.startswith(b"%PDF")
            full_bytes = len(r.content) if hasattr(r, 'content') else 0
            print(f"{name:45s} {str(r.status_code):6s} {full_bytes:>10d}  {is_pdf}")
            if r.status_code == 200 and is_pdf:
                ok_count += 1
        except Exception as e:
            print(f"{name:45s} ERROR  {type(e).__name__}: {str(e)[:60]}")
        time.sleep(0.3)
    print(f"\n  EES PDFs that responded with valid PDF: {ok_count}/{len(EES_PDFS)}")


def main():
    s = requests.Session()
    print("=== Session warmup ===")
    r0 = s.get("https://www.mca.gov.in/", headers=H_BROWSER, timeout=20, verify=False)
    print(f"  homepage: {r0.status_code} ({len(r0.content)} bytes)")
    probe_mca_portal(s)
    probe_taxguru(s)
    probe_ees()


if __name__ == "__main__":
    main()
