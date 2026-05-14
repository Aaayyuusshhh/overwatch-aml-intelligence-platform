"""
scrapers/nfra_orders.py — NFRA Orders Under Section 132(4)  (#89)

Source: https://nfra.gov.in/document-category/orders/   (8 pages)

Each order entry has:
  Title  | Date  | View / Download
where Title follows a stable pattern:
  "Order in the matter of <Entity> [against CA <Auditor>] for [FY ...]"
or
  "Errata - Order in the matter of <Entity> ..."

We extract:
  name           - the auditor (preferred) or entity from the title
  details        - full title + order date
  document_url   - direct CDN PDF link from the View / Download cell
  detail_page_url- listing page (orders index)

NOTE: NFRA already has a sibling source #88 (Debarments) at 103 records
which was scraped by the generic html engine. That page is a single
table on /debar/. The orders page #89 is paginated (8 pages) which the
generic engine can't iterate; hence this dedicated scraper.

Static fetch is sufficient — the listing page is server-rendered HTML
(no JS shell), confirmed via probe May 2026.
"""

import csv
import os
import re
import time
from datetime import datetime
from urllib.parse import urljoin

import requests
import urllib3
from bs4 import BeautifulSoup
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUTPUT_FILE  = os.path.join(PROJECT_ROOT, "data", "nfra_orders_89.csv")

BASE_URL = "https://nfra.gov.in/document-category/orders/"
DETAIL_PAGE_URL = BASE_URL

UA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")
HEADERS = {"User-Agent": UA, "Accept-Language": "en-US,en;q=0.9"}

POLITENESS_SECONDS = 1.5
PAGE_TIMEOUT       = 30
MAX_PAGES          = 25  # safety cap; actual is ~8

CSV_FIELDS = [
    "source_agency", "source_list", "case_unit",
    "name", "father_name", "date_of_birth", "gender",
    "address", "reward_amount", "details",
    "has_document", "document_url", "detail_page_url",
    "interpol_notice_id", "link_kind", "scraped_at",
    "enrichment_status",
]


def _session():
    s = requests.Session()
    retry = Retry(total=4, backoff_factor=1.5,
                  status_forcelist=(500, 502, 503, 504),
                  allowed_methods=("GET",), raise_on_status=False)
    s.mount("https://", HTTPAdapter(max_retries=retry))
    s.headers.update(HEADERS)
    return s


# --------------------------------------------------------------------------
# Title parser
# --------------------------------------------------------------------------
_M_AGAINST = re.compile(r"against\s+(?:CA\s+|M/s\.?\s*|Mr\.?\s+|Ms\.?\s+)?"
                        r"([^,/\(]{3,80}?)(?:\s+for\s+(?:the\s+)?FY|\s+for\s+FY|,|\(|$)",
                        re.I)
_M_MATTER  = re.compile(r"matter\s+of\s+(?:M/s\.?\s*)?"
                        r"([^,]{3,80}?)(?:\s+against|\s+for\s+(?:the\s+)?FY|\s+for\s+FY|,|\(|$)",
                        re.I)


def parse_title(title):
    """Return (auditor_or_None, entity_or_None) extracted from the
    canonical NFRA title structure. Either may be None."""
    auditor = None
    entity  = None
    t = title.replace("’", "'").replace("–", "-")
    m = _M_AGAINST.search(t)
    if m:
        auditor = m.group(1).strip().rstrip(",").strip()
    m = _M_MATTER.search(t)
    if m:
        entity = m.group(1).strip().rstrip(",").strip()
    return auditor, entity


# --------------------------------------------------------------------------
# Page fetch + parse
# --------------------------------------------------------------------------
def _page_url(page_num):
    if page_num <= 1:
        return BASE_URL
    return f"{BASE_URL}page/{page_num}"


def _fetch_page(session, page_num):
    url = _page_url(page_num)
    r = session.get(url, timeout=PAGE_TIMEOUT, verify=False)
    if r.status_code != 200:
        print(f"  page {page_num}: http {r.status_code}")
        return None
    if len(r.text) < 5000:
        print(f"  page {page_num}: tiny body {len(r.text)}; likely end of pagination")
        return None
    return r.text


def _parse_page(html, scraped_at):
    soup = BeautifulSoup(html, "html.parser")
    out = []
    # Page should have exactly one data-bearing <table>
    tables = soup.find_all("table")
    target = None
    for t in tables:
        rows = t.find_all("tr")
        if not rows:
            continue
        header = " ".join(c.get_text(" ", strip=True).lower()
                          for c in rows[0].find_all(["td", "th"]))
        if "title" in header and "date" in header:
            target = t
            break
    if target is None:
        return []
    rows = target.find_all("tr")
    for tr in rows[1:]:
        tds = tr.find_all("td")
        if len(tds) < 3:
            continue
        title = tds[0].get_text(" ", strip=True)
        date  = tds[1].get_text(" ", strip=True)
        dl_cell = tds[2]
        anchor = dl_cell.find("a", href=True)
        doc_url = ""
        if anchor:
            doc_url = urljoin(BASE_URL, anchor["href"].strip())
        if not title:
            continue
        auditor, entity = parse_title(title)
        # Preferred name: the auditor is the actor sanctioned; falls
        # back to the entity / first matter, else the raw title.
        name = auditor or entity or title[:120]
        details_parts = [f"Date: {date}" if date else None,
                         f"Title: {title}",
                         f"Entity: {entity}" if entity else None,
                         f"Auditor: {auditor}" if auditor else None]
        details = " | ".join(p for p in details_parts if p)
        out.append({
            "source_agency":   "NFRA",
            "source_list":     "Orders Under Section 132(4)",
            "case_unit":       "",
            "name":            name,
            "father_name":     "",
            "date_of_birth":   "",
            "gender":          "",
            "address":         "",
            "reward_amount":   "",
            "details":         details[:1500],
            "has_document":    "Yes" if doc_url else "No",
            "document_url":    doc_url,
            "detail_page_url": DETAIL_PAGE_URL,
            "interpol_notice_id": "",
            "link_kind":       "constructed",
            "scraped_at":      scraped_at,
            "enrichment_status": "",
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


def _save_recipe():
    try:
        from utils.request_recipes import save_recipe
        save_recipe("nfra_orders_89", {
            "source_id": "nfra_orders_89",
            "url": BASE_URL,
            "method": "GET",
            "headers": HEADERS,
            "params": {},
            "body": None,
            "cookies": {},
            "response_type": "html",
            "notes": "NFRA orders under Section 132(4). Paginate via "
                     "BASE_URL + '/page/N' (1..~8).",
        })
    except Exception:
        pass


def scrape():
    s = _session()
    scraped_at = datetime.now().strftime("%Y-%m-%dT%H:%M:%S")
    all_recs = []
    seen_docs = set()
    for page in range(1, MAX_PAGES + 1):
        html = _fetch_page(s, page)
        if html is None:
            break
        recs = _parse_page(html, scraped_at)
        # Dedup by document_url across pages (in case of overlap).
        new = 0
        for r in recs:
            key = (r["document_url"] or r["details"]).lower()
            if key in seen_docs:
                continue
            seen_docs.add(key)
            all_recs.append(r)
            new += 1
        print(f"  page {page:>2}  records={len(recs):>3}  new={new:>3}  total={len(all_recs)}")
        if new == 0:
            break
        time.sleep(POLITENESS_SECONDS)
    s.close()
    return all_recs


def run():
    print("=" * 60)
    print("NFRA Orders Under Section 132(4) scraper (#89)")
    print("=" * 60)
    recs = scrape()
    if not recs:
        raise RuntimeError("NFRA orders: zero records — page structure may have changed")
    _save_csv(recs, OUTPUT_FILE)
    _save_recipe()
    print("\nfirst 3 rows:")
    for r in recs[:3]:
        print(f"  name={r['name']!r}")
        print(f"    {r['details'][:130]}")
    print("\nlast 3 rows:")
    for r in recs[-3:]:
        print(f"  name={r['name']!r}")
        print(f"    {r['details'][:130]}")
    return recs


if __name__ == "__main__":
    run()
