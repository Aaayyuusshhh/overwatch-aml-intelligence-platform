"""
Hong Kong scrapers: HKMA (Disciplinary + Enforcement) and ICAC press releases.
SFC HK enforcement-news endpoint 404s and /Enforcement landing is nav-only;
skipped here (logged as blocked).
"""
import csv, os, re, time
from datetime import datetime, timezone
from urllib.parse import urljoin
import requests
from bs4 import BeautifulSoup

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA = os.path.join(PROJECT_ROOT, "data")
LOG = os.path.join(PROJECT_ROOT, "logs", "scrape_session_20260520.log")
HDRS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0) Chrome/124",
        "Accept-Language": "en;q=0.9"}
HEADER = ["source_agency", "source_list", "case_unit", "name", "father_name",
          "date_of_birth", "gender", "address", "reward_amount", "details",
          "has_document", "document_url", "detail_page_url", "interpol_notice_id",
          "link_kind", "scraped_at", "enrichment_status"]
NOW = datetime.now(timezone.utc).isoformat()


def log(tag, msg):
    line = f"{datetime.now().isoformat(timespec='seconds')} [{tag}] {msg}"
    print(line, flush=True)
    with open(LOG, "a") as f:
        f.write(line + "\n")


def write_csv(rows, fname):
    path = os.path.join(DATA, fname)
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=HEADER)
        w.writeheader()
        w.writerows(rows)
    return path, len(rows)


def base_row(agency, lst, name, **kw):
    r = {"source_agency": agency, "source_list": lst, "case_unit": "",
         "name": name, "father_name": "", "date_of_birth": "", "gender": "",
         "address": "", "reward_amount": "", "details": "", "has_document": "",
         "document_url": "", "detail_page_url": "", "interpol_notice_id": "",
         "link_kind": "", "scraped_at": NOW, "enrichment_status": ""}
    r.update(kw)
    return r


# --------------------------- HKMA --------------------------------------------
def scrape_hkma():
    URLS = [
        ("AML/CFT Disciplinary",
         "https://www.hkma.gov.hk/eng/key-functions/banking/anti-money-laundering-and-counter-financing-of-terrorism/disciplinary-actions/"),
        ("Banking Conduct Enforcement",
         "https://www.hkma.gov.hk/eng/key-functions/banking/banking-conduct-supervision/enforcement-actions/"),
    ]
    rows = []
    for sublist, url in URLS:
        r = requests.get(url, headers=HDRS, timeout=25)
        if r.status_code != 200:
            log("hkma", f"{sublist}: status={r.status_code}")
            continue
        soup = BeautifulSoup(r.text, "lxml")
        items = soup.select(".related-information-item")
        for it in items:
            date_el = it.select_one(".related-information-date")
            text_el = it.select_one(".related-information-text") or it.select_one(".icon-link-text") or it
            link_el = it.find("a")
            date = date_el.get_text(" ", strip=True) if date_el else ""
            text = text_el.get_text(" ", strip=True)
            href = urljoin(url, link_el["href"]) if link_el and link_el.get("href") else url
            # PDF link?
            pdf = link_el["href"] if link_el and link_el.get("href", "").lower().endswith(".pdf") else ""
            if not text or len(text) < 4:
                continue
            rows.append(base_row(
                "Hong Kong Monetary Authority (HKMA)",
                f"Enforcement / Disciplinary Actions ({sublist})",
                text,
                details=f"date: {date} | sublist: {sublist}",
                detail_page_url=href,
                document_url=urljoin(url, pdf) if pdf else "",
                has_document="Yes" if pdf else "",
            ))
        log("hkma", f"{sublist}: {len(items)} items")
    return rows


# --------------------------- ICAC press releases -----------------------------
def scrape_icac():
    """ICAC press index lists ~10 items per page; paginate via ?p=N or similar.
    Probe a few pagination patterns; fall back to single page if unique."""
    rows = []
    seen = set()
    BASE = "https://www.icac.org.hk/en/p/press/"
    # The site uses /en/p/press/index.html with pagination links inside.
    # First fetch the index and gather pagination URLs from the .pagenum block.
    r = requests.get(BASE + "index.html", headers=HDRS, timeout=25)
    soup = BeautifulSoup(r.text, "lxml")

    def parse_page(soup, src_url):
        added = 0
        for it in soup.select(".pressItem"):
            link = it.find("a")
            href = urljoin(src_url, link["href"]) if link and link.get("href") else src_url
            date_el = it.select_one(".date")
            details_el = it.select_one(".details") or it.select_one(".hd")
            date = date_el.get_text(" ", strip=True) if date_el else ""
            text = details_el.get_text(" ", strip=True) if details_el else ""
            if not text or text in seen:
                continue
            seen.add(text)
            rows.append(base_row(
                "Independent Commission Against Corruption (ICAC Hong Kong)",
                "Press Releases (Corruption Cases)",
                text,
                details=f"date: {date}", detail_page_url=href,
            ))
            added += 1
        return added

    parse_page(soup, BASE + "index.html")
    # discover pagination links
    pages = set()
    for a in soup.select(".pagenum a, .pagination a, a[href*='index'][href*='.html']"):
        h = a.get("href", "")
        if h and "index" in h and ".html" in h and h != "index.html":
            pages.add(urljoin(BASE, h))
    # also try /index_N.html / /index-N.html
    for n in range(2, 40):
        pages.add(BASE + f"index_{n}.html")
        pages.add(BASE + f"index-{n}.html")
        pages.add(BASE + f"index{n}.html")
    tried = 0
    for url in sorted(pages):
        tried += 1
        if tried > 50:
            break
        try:
            r = requests.get(url, headers=HDRS, timeout=15)
            if r.status_code != 200 or len(r.text) < 5000:
                continue
            added = parse_page(BeautifulSoup(r.text, "lxml"), url)
            if added:
                log("icac", f"{url} added {added} (cum={len(rows)})")
        except Exception:
            continue
        time.sleep(0.3)
    log("icac", f"{len(rows)} press releases total")
    return rows


def main():
    p, n = write_csv(scrape_hkma(), "hkma_enforcement.csv"); log("write", f"{n} -> {p}")
    p, n = write_csv(scrape_icac(), "icac_hk_corruption.csv"); log("write", f"{n} -> {p}")


if __name__ == "__main__":
    main()
