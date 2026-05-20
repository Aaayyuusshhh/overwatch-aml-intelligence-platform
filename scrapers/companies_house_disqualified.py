"""
Companies House Disqualified Officers scraper.

Uses the public find-and-update front-end (no API key needed). Pagination is
fixed at 20 results/page; we iterate q over a..z, paginate per letter with an
early-stop when results vanish or hit MAX_PAGES_PER_LETTER, and dedup by URL.
"""
import csv, os, re, sys, time
from datetime import datetime, timezone
from urllib.parse import urljoin
import requests
from bs4 import BeautifulSoup

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(PROJECT_ROOT, "data", "companies_house_disqualified_directors.csv")
LOG = os.path.join(PROJECT_ROOT, "logs", "scrape_session_20260520.log")

BASE = "https://find-and-update.company-information.service.gov.uk"
URL = BASE + "/search/disqualified-officers"
HDRS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/124 Safari/537.36",
        "Accept-Language": "en-GB,en;q=0.9"}
MAX_PAGES_PER_LETTER = int(os.environ.get("MAX_PAGES_PER_LETTER", "30"))
SLEEP = float(os.environ.get("SLEEP", "0.8"))
HEADER = ["source_agency", "source_list", "case_unit", "name", "father_name",
          "date_of_birth", "gender", "address", "reward_amount", "details",
          "has_document", "document_url", "detail_page_url", "interpol_notice_id",
          "link_kind", "scraped_at", "enrichment_status"]


def log(msg):
    line = f"{datetime.now().isoformat(timespec='seconds')} [ch_disq] {msg}"
    print(line, flush=True)
    with open(LOG, "a") as f:
        f.write(line + "\n")


def parse_page(html):
    """Yield {url,name,kind,detail_text} for each hit on the search page."""
    soup = BeautifulSoup(html, "lxml")
    items = []
    for li in soup.select("li.type-disqualified-officer, li.appointment-1"):
        a = li.find("a", class_="govuk-link")
        if not a:
            continue
        url = urljoin(BASE, a.get("href", ""))
        name = a.get_text(" ", strip=True)
        kind = "corporate" if "/corporate/" in url else "natural"
        meta = " | ".join(dd.get_text(" ", strip=True) for dd in li.find_all(["dd", "p"]))
        items.append({"url": url, "name": name, "kind": kind, "meta": meta})
    if not items:
        # fallback selector
        for a in soup.select('a.govuk-link[href^="/disqualified-officers/"]'):
            url = urljoin(BASE, a.get("href"))
            items.append({"url": url, "name": a.get_text(" ", strip=True),
                          "kind": "corporate" if "/corporate/" in url else "natural", "meta": ""})
    return items


def main():
    seen = set()
    rows = []
    now = datetime.now(timezone.utc).isoformat()
    s = requests.Session()
    s.headers.update(HDRS)

    for letter in "abcdefghijklmnopqrstuvwxyz":
        empty_streak = 0
        for page in range(1, MAX_PAGES_PER_LETTER + 1):
            try:
                r = s.get(URL, params={"q": letter, "page": page}, timeout=25)
            except Exception as e:
                log(f"!! q={letter} p={page} {type(e).__name__}: {e}")
                time.sleep(2)
                continue
            if r.status_code != 200:
                log(f"!! q={letter} p={page} status={r.status_code}")
                break
            items = parse_page(r.text)
            new = 0
            for it in items:
                if it["url"] in seen:
                    continue
                seen.add(it["url"])
                new += 1
                details_parts = [f"kind: {it['kind']}", f"source_url: {it['url']}"]
                if it["meta"]:
                    details_parts.append(f"meta: {it['meta']}")
                rows.append({
                    "source_agency": "Companies House",
                    "source_list": "Disqualified Directors Register",
                    "case_unit": "", "name": it["name"], "father_name": "",
                    "date_of_birth": "", "gender": "", "address": "",
                    "reward_amount": "",
                    "details": " | ".join(details_parts),
                    "has_document": "", "document_url": "",
                    "detail_page_url": it["url"], "interpol_notice_id": "",
                    "link_kind": "", "scraped_at": now, "enrichment_status": "",
                })
            if not items:
                empty_streak += 1
                if empty_streak >= 2:
                    log(f"   q={letter} stopped at p={page} (empty)")
                    break
            else:
                empty_streak = 0
            if page % 10 == 0:
                log(f"   q={letter} p={page} cum_unique={len(rows)}")
            time.sleep(SLEEP)
        log(f"-- letter '{letter}' done, cum_unique={len(rows)}")
        # safety cap
        if len(rows) >= 20000:
            log("hard cap 20000 hit, stopping")
            break

    with open(OUT, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=HEADER)
        w.writeheader()
        w.writerows(rows)
    log(f"DONE: {len(rows)} unique records -> {OUT}")


if __name__ == "__main__":
    main()
