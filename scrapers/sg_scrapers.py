"""
Singapore scrapers: MAS Enforcement, ACRA Struck-Off (PDF registry),
SGX Disciplinary, CPIB press releases.

MAS Investor Alert List is search-only (no public bulk endpoint) — logged
as blocked, not scraped here.
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


# --------------------------- MAS Enforcement ---------------------------------
def scrape_mas_enforcement():
    URL = "https://www.mas.gov.sg/regulation/enforcement/enforcement-actions"
    rows = []
    seen = set()
    page = 1
    consecutive_repeats = 0
    while page <= 200 and consecutive_repeats < 2:
        r = requests.get(URL, params={"page": page}, headers=HDRS, timeout=25)
        if r.status_code != 200:
            log("mas_enf", f"page={page} status={r.status_code}, stopping")
            break
        soup = BeautifulSoup(r.text, "lxml")
        tab = soup.find("table")
        if not tab:
            break
        new_this_page = 0
        for tr in tab.find_all("tr"):
            cells = tr.find_all("td")
            if len(cells) < 4:
                continue
            date = cells[0].get_text(" ", strip=True)
            person = cells[1].get_text(" ", strip=True)
            action = cells[2].get_text(" ", strip=True)
            title = cells[3].get_text(" ", strip=True)
            link_el = cells[3].find("a")
            href = urljoin("https://www.mas.gov.sg", link_el["href"]) if link_el and link_el.get("href") else URL
            key = (date, person, title)
            if key in seen:
                continue
            seen.add(key)
            new_this_page += 1
            rows.append(base_row(
                "Monetary Authority of Singapore (MAS)", "Enforcement Actions",
                person,
                details=f"issue_date: {date} | action_type: {action} | title: {title}",
                detail_page_url=href,
            ))
        if new_this_page == 0:
            consecutive_repeats += 1
        else:
            consecutive_repeats = 0
        if page % 5 == 0 or new_this_page == 0:
            log("mas_enf", f"page={page} new={new_this_page} cum={len(rows)}")
        page += 1
        time.sleep(0.4)
    return rows


# --------------------------- ACRA Struck-Off ---------------------------------
def scrape_acra_struck_off():
    """PDFs are vector-rendered (pdfplumber sees 0 chars) so we register one row
    per published gazette PDF as a document_url pointer. Covers all years 2020-2026."""
    rows = []
    for year in range(2020, 2027):
        url = f"https://www.acra.gov.sg/resources/gazettes/final-gazette/struck-off-final-gazette-{year}/"
        r = requests.get(url, headers=HDRS, timeout=25)
        if r.status_code != 200:
            log("acra", f"year {year} status={r.status_code}")
            continue
        pdfs = sorted({h for h in re.findall(r'href="([^"]+\.pdf)"', r.text)})
        for pdf in pdfs:
            # derive a readable name from filename
            fn = pdf.rsplit("/", 1)[-1].rsplit(".", 1)[0].replace("%20", " ").replace("_", " ").replace("-", " ")
            fn = re.sub(r"\s+", " ", fn).strip().title()
            rows.append(base_row(
                "Accounting and Corporate Regulatory Authority (ACRA)",
                "Struck-Off Companies Register (Final Gazette)",
                fn,
                case_unit=str(year),
                details=f"gazette_year: {year} | source: ACRA Final Gazette PDF",
                has_document="Yes", document_url=pdf, detail_page_url=url,
                enrichment_status="pdf_ocr_required",
            ))
        log("acra", f"year {year}: {len(pdfs)} PDFs")
    return rows


# --------------------------- SGX Disciplinary (Playwright) -------------------
def scrape_sgx_disciplinary():
    """SGX RegCo is a SPA shell — render with Playwright."""
    from playwright.sync_api import sync_playwright
    URL = "https://regco.sgx.com/public-disciplinary-actions"
    rows = []
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        ctx = browser.new_context(user_agent=HDRS["User-Agent"],
                                  viewport={"width": 1400, "height": 900},
                                  locale="en-SG", timezone_id="Asia/Singapore")
        page = ctx.new_page()
        page.goto(URL, wait_until="domcontentloaded", timeout=45000)
        try:
            page.wait_for_load_state("networkidle", timeout=20000)
        except Exception:
            pass
        # scroll to load more
        for _ in range(6):
            page.evaluate("window.scrollBy(0, document.body.scrollHeight)")
            time.sleep(1.2)
        time.sleep(2)
        html = page.content()
        soup = BeautifulSoup(html, "lxml")
        # Try multiple selectors that SGX RegCo typically uses
        items = (soup.select("article, .article-item, .card, .listing-item, .news-listing__item, .l-card, [class*='disciplinary']"))
        # If no clear class, fall back to any anchor under a /public-disciplinary-actions/ slug
        anchors = soup.select('a[href*="public-disciplinary-actions"], a[href*="/disciplinary"]')
        # also pull h2/h3 + sibling text as fallback
        if not items and anchors:
            for a in anchors:
                href = urljoin(URL, a.get("href", ""))
                if href == URL or "regco.sgx.com/public-disciplinary-actions" not in href:
                    continue
                name = a.get_text(" ", strip=True)
                if not name or len(name) < 5:
                    continue
                rows.append(base_row(
                    "Singapore Exchange (SGX)", "Public Disciplinary Actions",
                    name, detail_page_url=href,
                ))
        else:
            for it in items:
                a = it.find("a")
                name = (a.get_text(" ", strip=True) if a else it.get_text(" ", strip=True))[:200].strip()
                href = urljoin(URL, a["href"]) if a and a.get("href") else URL
                if not name or "disciplinary" in name.lower() and "action" in name.lower() and len(name) < 30:
                    continue
                rows.append(base_row(
                    "Singapore Exchange (SGX)", "Public Disciplinary Actions",
                    name, detail_page_url=href,
                ))
        ctx.close()
        browser.close()
    # dedup by detail_page_url
    seen = set()
    uniq = []
    for r in rows:
        if r["detail_page_url"] in seen:
            continue
        seen.add(r["detail_page_url"])
        uniq.append(r)
    log("sgx", f"{len(uniq)} disciplinary actions")
    return uniq


# --------------------------- CPIB press releases -----------------------------
def scrape_cpib():
    """Sitemap has 1,239 locs incl. press releases. Filter to /press-releases/ slugs.
    Use slug as readable name and URL as detail_page_url; no per-page fetch."""
    sm = requests.get("https://www.cpib.gov.sg/sitemap.xml", headers=HDRS, timeout=25).text
    locs = re.findall(r"<loc>([^<]+)</loc>", sm)
    pr = [u for u in locs if "/press-room/press-releases/" in u and not u.rstrip("/").endswith("press-releases")]
    rows = []
    for u in pr:
        slug = u.rstrip("/").rsplit("/", 1)[-1]
        # gracefully decode &apos; etc.
        slug = (slug.replace("&apos;", "'").replace("&amp;", "&"))
        name = slug.replace("-", " ").strip().title()
        rows.append(base_row(
            "Corrupt Practices Investigation Bureau (CPIB)", "Corruption Cases (Press Releases)",
            name, details=f"slug: {slug}", detail_page_url=u,
        ))
    log("cpib", f"{len(rows)} press releases")
    return rows


def main():
    p, n = write_csv(scrape_mas_enforcement(), "mas_revoked_licences.csv"); log("write", f"{n} -> {p}")
    p, n = write_csv(scrape_acra_struck_off(), "acra_struck_off_companies.csv"); log("write", f"{n} -> {p}")
    p, n = write_csv(scrape_sgx_disciplinary(), "sgx_disciplinary_actions.csv"); log("write", f"{n} -> {p}")
    p, n = write_csv(scrape_cpib(), "cpib_corruption_cases.csv"); log("write", f"{n} -> {p}")


if __name__ == "__main__":
    main()
