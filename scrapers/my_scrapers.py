"""
Malaysia scrapers: SC Investor Alerts (direct JSON API), SC Enforcement Actions
(requests + custom selectors), BNM Enforcement Actions (Playwright; site has AWS
WAF challenge that pure requests fails). Bursa Disciplinary is CF-blocked (skip).
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


# --------------------------- SC MY Investor Alerts (API) ---------------------
def scrape_sc_my_investor_alerts():
    API = "https://investmentcheckerapi.sc.com.my/Cache?_rt=unauthorised"
    r = requests.get(API, headers=HDRS, timeout=60)
    r.raise_for_status()
    data = r.json()
    rows = []
    for item in data:
        # Each record: cacheId, searchTitle, guid, resultTitle, resultType, resultCount
        title = item.get("searchTitle") or item.get("resultTitle") or ""
        if not title.strip():
            continue
        detail = (f"result_type: {item.get('resultType','')} | "
                  f"result_title: {item.get('resultTitle','')} | "
                  f"result_count: {item.get('resultCount','')} | "
                  f"guid: {item.get('guid','')} | cache_id: {item.get('cacheId','')}")
        rows.append(base_row(
            "Securities Commission Malaysia (SC)", "Investor Alert List",
            title, details=detail,
            detail_page_url="https://www.sc.com.my/investor-alert-list",
        ))
    log("sc_my_inv", f"{len(rows)} investor alerts")
    return rows


# --------------------------- SC MY Enforcement -------------------------------
def scrape_sc_my_enforcement():
    URL = "https://www.sc.com.my/regulation/enforcement/actions"
    rows = []
    seen = set()
    for page in range(1, 60):
        params = {} if page == 1 else {"page": page}
        r = requests.get(URL, params=params, headers=HDRS, timeout=25)
        if r.status_code != 200:
            log("sc_my_enf", f"page {page} status={r.status_code} stop")
            break
        soup = BeautifulSoup(r.text, "lxml")
        # The SC site renders enforcement-action items with class containing 'so-thumbnail'
        items = soup.select(".so-thumbnail, .aps-0031-so-wrapper, .aps-0036-so-wrapper")
        new = 0
        for it in items:
            a = it.find("a")
            href = urljoin(URL, a["href"]) if a and a.get("href") else ""
            title_el = it.find(["h2", "h3", "h4", "h5"]) or a
            title = (title_el.get_text(" ", strip=True) if title_el else "")[:300]
            if not title or len(title) < 5 or title in seen:
                continue
            seen.add(title)
            # snippet text
            snippet = it.get_text(" ", strip=True).replace(title, "", 1).strip(" |-")
            snippet = re.sub(r"\s+", " ", snippet)[:500]
            rows.append(base_row(
                "Securities Commission Malaysia (SC)", "Enforcement Actions",
                title,
                details=f"summary: {snippet}" if snippet else "",
                detail_page_url=href or URL,
            ))
            new += 1
        if new == 0:
            log("sc_my_enf", f"page {page} no new, stop (cum={len(rows)})")
            break
        time.sleep(0.4)
    return rows


# --------------------------- BNM (Playwright) --------------------------------
def scrape_bnm_enforcement():
    """BNM has AWS WAF; pure requests gets 2KB shell. Use Playwright to render."""
    from playwright.sync_api import sync_playwright
    URL = "https://www.bnm.gov.my/enforcement-actions"
    rows = []
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        ctx = browser.new_context(user_agent=HDRS["User-Agent"],
                                  viewport={"width": 1400, "height": 900},
                                  locale="en-MY")
        page = ctx.new_page()
        try:
            page.goto(URL, wait_until="domcontentloaded", timeout=60000)
            try:
                page.wait_for_load_state("networkidle", timeout=25000)
            except Exception:
                pass
            for _ in range(8):
                page.evaluate("window.scrollBy(0, document.body.scrollHeight)")
                time.sleep(1.0)
            time.sleep(2)
            html = page.content()
        finally:
            ctx.close()
            browser.close()
    soup = BeautifulSoup(html, "lxml")
    # BNM uses Liferay; enforcement items are typically tabular or in journal-article blocks
    # Try multiple selectors
    tables = soup.find_all("table")
    items = []
    for tab in tables:
        for tr in tab.find_all("tr"):
            cells = tr.find_all(["td"])
            if len(cells) >= 2:
                texts = [c.get_text(" ", strip=True) for c in cells]
                items.append(("table", texts))
    # Also try card/article patterns
    for sel in [".portlet-body article", "div.row.entry", "article", ".accordion-item", ".panel-body"]:
        for el in soup.select(sel)[:200]:
            txt = el.get_text(" ", strip=True)
            if len(txt) > 40 and len(txt) < 1500 and any(k in txt.lower() for k in ("imposed","penalty","compound","action","section","fine","bank","sdn bhd")):
                items.append((sel, [txt]))
    # Dedup by text fingerprint
    seen = set()
    for src, vals in items:
        text = " | ".join(vals)
        fp = re.sub(r"\s+", " ", text)[:200]
        if fp in seen:
            continue
        seen.add(fp)
        # heuristic: first cell is likely date or name; use longest as name
        name = max(vals, key=len)[:200] if vals else text[:200]
        rows.append(base_row(
            "Bank Negara Malaysia (BNM)", "Enforcement Actions",
            name, details=f"raw: {text[:1000]} | source_block: {src}",
            detail_page_url=URL,
        ))
    log("bnm", f"{len(rows)} enforcement entries (heuristic)")
    return rows


def main():
    p, n = write_csv(scrape_sc_my_investor_alerts(), "sc_malaysia_investor_alerts.csv"); log("write", f"{n} -> {p}")
    p, n = write_csv(scrape_sc_my_enforcement(), "sc_malaysia_enforcement.csv"); log("write", f"{n} -> {p}")
    p, n = write_csv(scrape_bnm_enforcement(), "bnm_enforcement_actions.csv"); log("write", f"{n} -> {p}")


if __name__ == "__main__":
    main()
