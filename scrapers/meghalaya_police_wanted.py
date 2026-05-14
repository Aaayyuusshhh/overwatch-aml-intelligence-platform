"""
scrapers/meghalaya_police_wanted.py — Meghalaya Police List of Wanted
Persons  (#217)

Source: https://megpolice.gov.in/wanted-person   (5 pages, ?page=0..4)

Page is a Drupal 7 view (`view-id-wanted_person`) rendering wanted
persons as <li class="views-row"> cards. Per-card structure (verified
May 2026):

  <li class="views-row ...">
    <div class="views-field views-field-field-image">
      <div class="field-content">
        <div class="col-sm-12 wanted-person">
          <div class="col-sm-2"><img src="<photo>" alt="<Name>" /></div>
          <div class="col-sm-10">
            <Name>
            <br /> <Case details, FIR, PS, sections>
            <br /> <a href="/<slug>">View details about <Name></a>
          </div>
        </div>
      </div>
    </div>
  </li>

Static fetch suffices — server-rendered HTML, no JS needed.

Public:
  run()          — entry point invoked by the orchestrator
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
OUTPUT_FILE  = os.path.join(PROJECT_ROOT, "data",
                            "mp_list_of_wanted_person_217.csv")

BASE_URL = "https://megpolice.gov.in/wanted-person"

UA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")
HEADERS = {"User-Agent": UA, "Accept-Language": "en-US,en;q=0.9"}

POLITENESS_SECONDS = 1.5
PAGE_TIMEOUT       = 30
MAX_PAGES          = 10  # safety cap; actual is 5

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


def _page_url(page_idx):
    """page=0 is the default landing; page=1..4 are subsequent."""
    if page_idx == 0:
        return BASE_URL
    return f"{BASE_URL}?page={page_idx}"


def _parse_card(card_div, scraped_at):
    """Parse one .wanted-person card. Returns a record dict or None."""
    img = card_div.find("img")
    col10 = card_div.find("div", class_="col-sm-10")
    if col10 is None:
        return None

    # The col-sm-10 contains: name<br/>details<br/><a>view details</a>.
    # Walk top-level children, split text on <br/> tokens.
    parts = []
    for child in col10.children:
        if getattr(child, "name", None) == "br":
            continue
        if getattr(child, "name", None) == "a":
            # skip the 'View details about ...' link text from parts
            continue
        s = (child.get_text(" ", strip=True)
             if hasattr(child, "get_text") else str(child).strip())
        if s:
            parts.append(s)

    # First part is the name; the rest is case detail.
    name = ""
    case = ""
    if parts:
        name = parts[0].strip()
        case = " | ".join(p for p in parts[1:] if p)
    # Fallback to img alt if textual name parse missed
    if not name and img and img.get("alt"):
        name = img["alt"].strip()
    if not name:
        return None

    photo_url = ""
    if img and img.get("src"):
        photo_url = urljoin(BASE_URL, img["src"].strip())

    detail_anchor = col10.find("a", href=True)
    detail_url = ""
    if detail_anchor:
        detail_url = urljoin(BASE_URL, detail_anchor["href"].strip())

    return {
        "source_agency":   "Meghalaya Police",
        "source_list":     "Wanted Persons",
        "case_unit":       "",
        "name":            name,
        "father_name":     "",
        "date_of_birth":   "",
        "gender":          "",
        "address":         "",
        "reward_amount":   "",
        "details":         case[:1500],
        "has_document":    "Yes" if photo_url else "No",
        "document_url":    photo_url,
        "detail_page_url": detail_url or BASE_URL,
        "interpol_notice_id": "",
        "link_kind":       "homepage_scan",
        "scraped_at":      scraped_at,
        "enrichment_status": "",
    }


def _fetch_page(session, page_idx):
    url = _page_url(page_idx)
    r = session.get(url, timeout=PAGE_TIMEOUT, verify=False)
    if r.status_code != 200:
        print(f"  page {page_idx}: http {r.status_code}")
        return None
    return r.text


def _parse_page(html, scraped_at):
    soup = BeautifulSoup(html, "html.parser")
    cards = soup.find_all("div", class_="wanted-person")
    out = []
    for c in cards:
        rec = _parse_card(c, scraped_at)
        if rec:
            out.append(rec)
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
        save_recipe("meghalaya_police_wanted_217", {
            "source_id": "mp_list_of_wanted_person_217",
            "url":   BASE_URL,
            "method": "GET",
            "headers": HEADERS,
            "params": {},
            "body": None, "cookies": {},
            "response_type": "html",
            "notes": "Meghalaya Police wanted persons. Drupal 7 view; "
                     "paginate with ?page=0..4. .wanted-person card "
                     "selector.",
        })
    except Exception:
        pass


def scrape():
    s = _session()
    scraped_at = datetime.now().strftime("%Y-%m-%dT%H:%M:%S")
    all_records = []
    seen_names = set()
    for page_idx in range(MAX_PAGES):
        html = _fetch_page(s, page_idx)
        if html is None:
            break
        records = _parse_page(html, scraped_at)
        new = 0
        for r in records:
            key = (r["name"].lower().strip(),
                   r["detail_page_url"].lower().strip())
            if key in seen_names:
                continue
            seen_names.add(key)
            all_records.append(r)
            new += 1
        print(f"  page {page_idx}  cards={len(records):>3}  new={new:>3}  "
              f"total={len(all_records)}")
        if new == 0:
            break
        time.sleep(POLITENESS_SECONDS)
    s.close()
    return all_records


def run():
    print("=" * 60)
    print("Meghalaya Police Wanted Persons scraper (#217)")
    print("=" * 60)
    records = scrape()
    if not records:
        raise RuntimeError("Meghalaya wanted: zero records — page "
                           "structure may have changed (looked for "
                           "div.wanted-person)")
    _save_csv(records, OUTPUT_FILE)
    _save_recipe()
    print("\nsample rows:")
    for r in records[:3]:
        print(f"  name={r['name']!r}")
        print(f"    details={r['details'][:120]!r}")
        print(f"    photo={r['document_url'][:100]!r}")
        print(f"    detail_page={r['detail_page_url'][:100]!r}")
    return records


if __name__ == "__main__":
    run()
