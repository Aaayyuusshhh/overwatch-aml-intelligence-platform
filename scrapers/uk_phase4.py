"""
Phase 4 scrapers: SFO Cases (gov.uk), ICO Enforcement (sitemap-driven),
SDT Judgments (the actual disciplinary register — solicitorstribunal.org.uk).

Each emits one CSV under data/. List-page parsing only, no per-case enrichment.
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
        "Accept-Language": "en-GB,en;q=0.9"}
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


def base_row(agency, lst, name, address="", details="", detail_url=""):
    return {
        "source_agency": agency, "source_list": lst, "case_unit": "",
        "name": name, "father_name": "", "date_of_birth": "", "gender": "",
        "address": address, "reward_amount": "", "details": details,
        "has_document": "", "document_url": "", "detail_page_url": detail_url,
        "interpol_notice_id": "", "link_kind": "", "scraped_at": NOW,
        "enrichment_status": "",
    }

# ----------------------------- SFO ----------------------------------
def scrape_sfo():
    r = requests.get("https://www.gov.uk/sfo-cases", headers=HDRS, timeout=25)
    soup = BeautifulSoup(r.text, "lxml")
    rows = []
    for li in soup.select("li.gem-c-document-list__item"):
        a = li.select_one("a.govuk-link") or li.find("a")
        if not a:
            continue
        href = urljoin("https://www.gov.uk", a.get("href", ""))
        name = a.get_text(" ", strip=True)
        # summary/desc text is the rest of the li
        desc = li.get_text(" ", strip=True).replace(name, "", 1).strip(" -–|")
        rows.append(base_row("Serious Fraud Office (SFO)", "SFO Cases",
                             name, "", f"summary: {desc}" if desc else "", href))
    log("sfo", f"{len(rows)} cases")
    return rows

# ----------------------------- ICO ----------------------------------
def scrape_ico():
    sm = requests.get("https://ico.org.uk/sitemap.xml", headers=HDRS, timeout=30).text
    locs = re.findall(r"<loc>([^<]+)</loc>", sm)
    urls = sorted({u for u in locs if "/action-weve-taken/enforcement/" in u
                   and u.rstrip("/").count("/") >= 6})  # exclude the hub page itself
    rows = []
    for u in urls:
        # URL form: /action-weve-taken/enforcement/<YYYY>/<MM>/<slug>/
        m = re.search(r"/enforcement/(\d{4})/(\d{2})/([^/]+)/?$", u)
        if not m:
            continue
        year, mon, slug = m.group(1), m.group(2), m.group(3)
        # derive a readable name from slug
        name = slug.replace("-", " ").strip().title()
        details = f"action_year: {year} | action_month: {mon} | slug: {slug}"
        rows.append(base_row("Information Commissioner's Office (ICO)",
                             "Enforcement Actions", name, "", details, u))
    log("ico", f"{len(rows)} enforcement actions (sitemap)")
    return rows

# ----------------------------- SDT ----------------------------------
def scrape_sdt():
    rows = []
    seen = set()
    base = "https://solicitorstribunal.org.uk/judgments/"
    sess = requests.Session(); sess.headers.update(HDRS)
    for page in range(1, 120):
        url = base if page == 1 else f"{base}page/{page}/"
        r = sess.get(url, timeout=20)
        if r.status_code != 200:
            log("sdt", f"page {page} stopped status={r.status_code}")
            break
        soup = BeautifulSoup(r.text, "lxml")
        # The /judgments/ page lists judgment entries — each a heading + link.
        # Use h2/h3/article with anchor href containing /case/<id>/.
        page_added = 0
        for a in soup.select('a[href*="/case/"]'):
            href = a.get("href", "")
            if "/case/" not in href:
                continue
            href = urljoin(base, href)
            if href in seen:
                continue
            seen.add(href)
            name = a.get_text(" ", strip=True)
            # surrounding text for date/details
            parent = a.find_parent(["article", "div", "li", "section"])
            ctx = parent.get_text(" ", strip=True)[:300] if parent else ""
            ctx = re.sub(r"\s+", " ", ctx)
            if not name or name.lower() in ("read more", "view", "details", "download"):
                # use the parent context first non-empty heading
                h = parent.find(["h2", "h3", "h4"]) if parent else None
                if h:
                    name = h.get_text(" ", strip=True)
            if not name:
                continue
            details = f"context: {ctx}" if ctx and ctx.lower() != name.lower() else ""
            rows.append(base_row("Solicitors Disciplinary Tribunal (SDT)",
                                 "SDT Judgments", name, "", details, href))
            page_added += 1
        if page % 10 == 0:
            log("sdt", f"page {page} cum_unique={len(rows)}")
        if page_added == 0 and page > 1:
            log("sdt", f"page {page} returned 0 new, stopping")
            break
        time.sleep(0.4)
    log("sdt", f"{len(rows)} judgments total")
    return rows


def main():
    sfo = scrape_sfo();  p, n = write_csv(sfo, "sfo_cases.csv"); log("write", f"{n} -> {p}")
    ico = scrape_ico();  p, n = write_csv(ico, "ico_enforcement.csv"); log("write", f"{n} -> {p}")
    sdt = scrape_sdt();  p, n = write_csv(sdt, "sra_disciplined_solicitors.csv"); log("write", f"{n} -> {p}")


if __name__ == "__main__":
    main()
