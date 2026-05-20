"""
Africa scrapers.

Working:
  - FIC South Africa: sitemap + canonical sanctions page
  - FRA Egypt:        insurance_decisions sub-sitemaps (paginated 1-4)
  - SARB:             best-effort with longer timeout
  - CBN Nigeria:      Azure-hosted AML page

Blocked (registered but not scraped):
  - EACC Kenya: ReadTimeout on both /cases/ paths and homepage
"""
import csv, os, re, time, requests, urllib3
from datetime import datetime, timezone
from urllib.parse import urljoin
from bs4 import BeautifulSoup

urllib3.disable_warnings()
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


def base(agency, lst, name, **kw):
    r = {h: "" for h in HEADER}
    r.update({"source_agency": agency, "source_list": lst, "name": name, "scraped_at": NOW})
    r.update(kw)
    return r


def write_csv(rows, fname):
    p = os.path.join(DATA, fname)
    with open(p, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=HEADER); w.writeheader(); w.writerows(rows)
    return p, len(rows)


# --------------------------- FIC South Africa --------------------------------
def scrape_fic_sa():
    """Use sitemap-derived sanction press-release URLs + canonical sanctions hub."""
    rows = []
    try:
        sm = requests.get("https://www.fic.gov.za/sitemap.xml", headers=HDRS, timeout=20).text
        locs = re.findall(r"<loc>([^<]+)</loc>", sm)
    except Exception as e:
        log("fic_sa", f"sitemap fetch failed: {e}"); return []
    # Press releases that name sanctioned entities (slug contains 'sanctioned')
    pr_urls = sorted({u for u in locs if "/sanctioned" in u.lower() or "sanction" in u.lower()})
    for u in pr_urls:
        slug = u.rstrip("/").rsplit("/", 1)[-1]
        if not slug or slug == "sanctions":
            continue
        name = slug.replace("-", " ").strip().title()
        rows.append(base(
            "Financial Intelligence Centre (FIC South Africa)", "Enforcement / Sanctions",
            name, details=f"source: sitemap | slug: {slug}", detail_page_url=u,
        ))
    # Add canonical sanctions hub as a registry row
    HUB = "https://www.fic.gov.za/compliance/supervision-and-enforcement/sanctions-issued-by-supervisory-bodies/"
    try:
        r = requests.get(HUB, headers=HDRS, timeout=20)
        soup = BeautifulSoup(r.text, "lxml")
        # Look for embedded supervisor names or table rows
        for tab in soup.find_all("table"):
            for tr in tab.find_all("tr"):
                cells = [c.get_text(" ", strip=True) for c in tr.find_all(["td", "th"])]
                if not cells or all(not c for c in cells):
                    continue
                name = max(cells, key=len)
                if len(name) < 5: continue
                rows.append(base(
                    "Financial Intelligence Centre (FIC South Africa)", "Enforcement / Sanctions",
                    name[:200], details=f"hub_table_row: {' | '.join(cells)[:500]}", detail_page_url=HUB,
                ))
    except Exception as e:
        log("fic_sa", f"hub fetch failed: {e}")
    # dedup
    seen=set(); uniq=[]
    for r in rows:
        k=r["name"].lower()
        if k in seen: continue
        seen.add(k); uniq.append(r)
    log("fic_sa", f"{len(uniq)} unique items")
    return uniq


# --------------------------- FRA Egypt ---------------------------------------
def scrape_fra_egypt():
    """Insurance decisions live in /wp-sitemap-posts-insurance_decisions-N.xml."""
    rows = []
    for n in range(1, 8):
        sm = f"https://fra.gov.eg/wp-sitemap-posts-insurance_decisions-{n}.xml"
        try:
            r = requests.get(sm, headers=HDRS, timeout=20, verify=False)
        except Exception:
            break
        if r.status_code != 200 or "<loc>" not in r.text:
            break
        urls = re.findall(r"<loc>([^<]+)</loc>", r.text)
        for u in urls:
            slug = u.rstrip("/").rsplit("/", 1)[-1]
            name = slug.replace("-", " ").strip()
            if not name:
                continue
            rows.append(base(
                "Financial Regulatory Authority (FRA Egypt)", "Enforcement Decisions",
                name[:200], details=f"source: insurance_decisions sitemap {n} | slug: {slug}",
                detail_page_url=u,
            ))
        log("fra_eg", f"sitemap {n}: +{len(urls)} cum_rows={len(rows)}")
    return rows


# --------------------------- SARB --------------------------------------------
def scrape_sarb():
    """SARB site is intermittent — retry sitemap once with long timeout."""
    rows = []
    try:
        r = requests.get("https://www.resbank.co.za/sitemap.xml", headers=HDRS, timeout=60)
        locs = re.findall(r"<loc>([^<]+)</loc>", r.text)
    except Exception as e:
        log("sarb", f"sitemap timeout: {e}"); return []
    # narrow filter: enforcement, regulatory-action, prudential-action, sanction
    cand = [u for u in locs if any(k in u.lower() for k in
            ("pa-enforcement", "pa-regulatory", "enforcement-action", "regulatory-action",
             "prudential-action", "sanction"))]
    for u in cand:
        slug = u.rstrip("/").rsplit("/", 1)[-1]
        name = slug.replace("-", " ").title()
        rows.append(base(
            "South African Reserve Bank (SARB)", "Enforcement Actions",
            name[:200], details=f"source: SARB sitemap", detail_page_url=u,
        ))
    log("sarb", f"{len(rows)} candidates")
    return rows


# --------------------------- CBN Nigeria -------------------------------------
def scrape_cbn():
    """Old supervision URLs 404; AML content moved to Azure-hosted page."""
    URL = "https://cenbankwebapi.azurewebsites.net/Documents/AML-CFT.html"
    PAGE = "https://www.cbn.gov.ng/Supervision/AMLCFTSanctionedEntities.asp"
    rows = []
    try:
        r = requests.get(URL, headers=HDRS, timeout=20, verify=False)
        soup = BeautifulSoup(r.text, "lxml")
        # AML content page lists circulars/PDFs with regulated entity actions
        # Capture any anchor text + href as a registry row
        for a in soup.find_all("a", href=True):
            text = a.get_text(" ", strip=True)
            if not text or len(text) < 6:
                continue
            href = a["href"]
            if href.startswith("#") or text.lower() in ("home", "back", "next", "previous"):
                continue
            rows.append(base(
                "Central Bank of Nigeria (CBN)", "AML/CFT Sanctioned Entities",
                text[:200], details=f"category: AML-CFT document",
                detail_page_url=urljoin(URL, href),
            ))
    except Exception as e:
        log("cbn", f"fetch failed: {e}")
    # Dedup
    seen=set(); uniq=[]
    for r in rows:
        if r["name"] in seen: continue
        seen.add(r["name"]); uniq.append(r)
    log("cbn", f"{len(uniq)} items")
    return uniq


def main():
    rows = scrape_fic_sa();    p, n = write_csv(rows, "fic_south_africa.csv");      log("write", f"{n} -> {p}")
    rows = scrape_fra_egypt(); p, n = write_csv(rows, "fra_egypt_enforcement.csv"); log("write", f"{n} -> {p}")
    rows = scrape_sarb();      p, n = write_csv(rows, "sarb_enforcement.csv");      log("write", f"{n} -> {p}")
    rows = scrape_cbn();       p, n = write_csv(rows, "cbn_nigeria_enforcement.csv"); log("write", f"{n} -> {p}")


if __name__ == "__main__":
    main()
