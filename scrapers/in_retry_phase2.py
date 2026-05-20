"""
Phase-2 retry scrapers (Indian sources): NIA Banned Terrorist Orgs +
ED Press Releases. Both proven reachable in recon — homepage URLs that
the prompt's original URLs missed.

Other Phase-2 targets (RBI ASP.NET error.aspx, NTPC Radware captcha,
GAIL/IOCL connection errors, UCO/PSB SPA shells) are logged as still
blocked and not attempted here.
"""
import csv, os, re, time
from datetime import datetime, timezone
from urllib.parse import urljoin
import requests
import urllib3
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


# --------------------- NIA Banned Terrorist Organisations -------------------
def scrape_nia_banned_orgs():
    URL = "https://nia.gov.in/banned-terrorist-organisations"
    r = requests.get(URL, headers=HDRS, timeout=25, verify=False)
    soup = BeautifulSoup(r.text, "lxml")
    rows = []
    # Pages of this kind usually list orgs as either table rows or h3-style entries.
    # First try tables; then fall back to a numbered list.
    items = []
    for tab in soup.find_all("table"):
        for tr in tab.find_all("tr"):
            cells = [c.get_text(" ", strip=True) for c in tr.find_all(["td", "th"])]
            if len(cells) >= 2 and cells[0] and cells[0].lower() != "name":
                items.append(("table", cells))
    if not items:
        # govt of India pages often use ordered <ol><li> for orgs lists
        for sel in ["ol li", "ul.list li", ".content li", ".views-field-body li"]:
            for li in soup.select(sel):
                txt = li.get_text(" ", strip=True)
                if 5 < len(txt) < 250 and not txt.lower().startswith(("read", "more", "click", "see")):
                    items.append(("li", [txt]))
            if items:
                break
    if not items:
        # numbered paragraph pattern e.g. "1. Babbar Khalsa International"
        body = soup.select_one(".region-content, .field-item, .node__content, main, body")
        if body:
            for ln in body.get_text("\n", strip=True).split("\n"):
                m = re.match(r"^\s*(\d{1,3})[.)]\s+(.{5,200})$", ln)
                if m:
                    items.append(("numbered", [m.group(2).strip()]))
    seen = set()
    for kind, vals in items:
        name = vals[0] if len(vals) == 1 else (max(vals, key=len) if all(vals) else vals[0])
        name = re.sub(r"\s+", " ", name).strip()
        if not name or name in seen or len(name) < 4:
            continue
        seen.add(name)
        details = f"source_block: {kind}" + (" | raw: " + " | ".join(vals[1:])[:300] if len(vals) > 1 else "")
        rows.append(base(
            "National Investigation Agency (NIA)",
            "Banned Terrorist Organisations",
            name[:200], details=details, detail_page_url=URL,
        ))
    log("nia_banned", f"{len(rows)} orgs")
    return rows


# --------------------------- ED Press Releases ------------------------------
def scrape_ed_press_releases():
    """ED publishes enforcement actions as press releases. The index page
    /media/press-release/ is paginated; each press release has the suspect's
    name in the title and a PDF document."""
    BASE = "https://enforcementdirectorate.gov.in"
    rows = []
    seen = set()
    for page in range(0, 25):  # try up to 25 pages; site uses 0-indexed
        url = f"{BASE}/media/press-release/" + (f"?page={page}" if page else "")
        try:
            r = requests.get(url, headers=HDRS, timeout=25, verify=False)
        except Exception as e:
            log("ed", f"page {page} ERR {type(e).__name__}"); break
        if r.status_code != 200:
            log("ed", f"page {page} status={r.status_code}"); break
        soup = BeautifulSoup(r.text, "lxml")
        # Press releases live as anchors to /media/press-release-documents/*.pdf
        # with descriptive titles in the link text.
        page_added = 0
        for a in soup.find_all("a", href=True):
            href = a["href"]
            text = a.get_text(" ", strip=True)
            if "/press-release-documents/" not in href and "press release" not in text.lower():
                continue
            if len(text) < 25 or text.lower() in ("press release",):
                continue
            full = urljoin(BASE, href)
            key = text.lower()[:120]
            if key in seen:
                continue
            seen.add(key)
            # Title is verbose; truncate gracefully for `name`.
            # Often the title encodes the suspect: "ED has arrested X" or
            # "ED has filed a prosecution complaint against Y".
            rows.append(base(
                "Enforcement Directorate (ED)", "Press Releases",
                text[:300],
                details=f"category: press release | page: {page}",
                detail_page_url=full,
                has_document="Yes" if href.lower().endswith(".pdf") else "",
                document_url=full if href.lower().endswith(".pdf") else "",
            ))
            page_added += 1
        if page_added == 0 and page > 0:
            log("ed", f"page {page} returned 0 new; stopping")
            break
        if page == 0 or page % 5 == 0:
            log("ed", f"page {page}: cum_unique={len(rows)}")
        time.sleep(0.4)
    return rows


def main():
    p, n = write_csv(scrape_nia_banned_orgs(),    "nia_banned_terrorist_organisations.csv")
    log("write", f"{n} -> {p}")
    p, n = write_csv(scrape_ed_press_releases(),  "ed_press_releases.csv")
    log("write", f"{n} -> {p}")


if __name__ == "__main__":
    main()
