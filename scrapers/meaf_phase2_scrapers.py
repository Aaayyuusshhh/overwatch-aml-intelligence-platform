"""
Middle East + Africa Phase-2 scrapers (Thursday 2026-05-22).

Targets:
  Working:
    - ADGM Abu Dhabi:  sitemap-driven announcements (fines / penalties / alerts)
    - FSCA South Africa: Latest-News page enforcement headlines
    - SEC Nigeria:    CFEA/APC/CREA/Litigation tables + Keep-Track updates list
  Blocked (registered as 'blocked'):
    - CMA Saudi Arabia: SharePoint SPA — content is JS-rendered
    - SAMA Saudi Arabia: SharePoint SPA — enforcement.aspx returns shell only
    - EFCC Nigeria: Cloudflare 403 challenge page on every URL
"""
import csv, os, re, time, requests, urllib3
from datetime import datetime, timezone
from urllib.parse import urljoin
from bs4 import BeautifulSoup

urllib3.disable_warnings()
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA = os.path.join(PROJECT_ROOT, "data")
LOG = os.path.join(PROJECT_ROOT, "logs", "scrape_session_20260522.log")
os.makedirs(os.path.dirname(LOG), exist_ok=True)

HDRS = {
    "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en;q=0.9",
}
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


# ============================== ADGM ABU DHABI ===============================
ADGM_ENF_KW = re.compile(
    r"(fine|fines|fined|penalt|penalties|sanction|sanctioned|breach|breaches|"
    r"alert|debar|disqualif|enforce|undertaking|contraven|imposes|impose|"
    r"administrative|disciplin|fraud|misrepresentation|trading-without|"
    r"exceeding-scope|purport)", re.I)
ADGM_SKIP_KW = re.compile(
    r"(commit-to-adgm|finance-week|enacts-electronic-transactions|"
    r"joins-the-ifiar|publishes-consultation|publishes-new-administrative-"
    r"regulations|sign-strategic-collaboration|cop16|aml-and-sanctions-rules)", re.I)


def _adgm_extract_amount(text):
    m = re.search(r"USD[\s ]*[\d,\.]+(?:\s*(?:million|billion|trillion|m))?", text, re.I)
    return m.group(0) if m else ""


def _adgm_extract_party(title):
    """Extract the named party from a typical ADGM announcement title.

    Patterns seen:
      'adgms-fsra-imposes-fines-of-usd-8-85-million-on-hayvn'
      'adgms-fsra-imposes-a-financial-penalty-of-usd-504000-on-aarna-capital-limited'
      'adgms-ra-fines-baker-tilly-and-its-audit-principal-for-audit-failings'
      'adgms-fsra-issues-alert-concerning-fraudulent-website-tungsten-me.com-and-tgst-me.com'
      'areej-al-noor-general-trading-llc-fined-usd-2000-for-trading-without-a-licence'
      'elmar-capital-spv-limited-fined-usd-5000-for-purporting-...'
    """
    t = title.strip().rstrip("/").rsplit("/", 1)[-1]
    raw = t
    # Pattern 1: ... on <party>
    m = re.search(r"(?:-on-|imposes-fines-to-|fines-)(.+?)(?:-for-|-and-its-|-imposing-|$)", t)
    if m and not raw.startswith(("adgms-fsra-issues-alert", "adgm-fsra-issues-alert")):
        party = m.group(1)
        if 5 < len(party) < 120 and not party.startswith(("usd-", "a-financial-penalty")):
            return party.replace("-", " ").strip().title(), raw
    # Pattern 2: <party>-fined-...
    m = re.match(r"^([a-z0-9][a-z0-9-]+?)-(?:fined|fines|fine)-usd", t)
    if m:
        return m.group(1).replace("-", " ").strip().title(), raw
    # Pattern 3: alert concerning website / fraudulent ...
    m = re.search(r"concerning-(?:fraudulent-)?(?:website-|scheme-)?(.+)$", t)
    if m:
        party = m.group(1)
        if 3 < len(party) < 120:
            return party.replace("-", " ").strip().title(), raw
    # Fallback: clean slug
    return t.replace("-", " ").strip().title(), raw


def scrape_adgm():
    rows = []
    try:
        sm = requests.get("https://www.adgm.com/sitemap.xml", headers=HDRS, timeout=30).text
    except Exception as e:
        log("adgm", f"sitemap fetch failed: {e}"); return []
    urls = re.findall(r"<loc>([^<]+)</loc>", sm)
    cand = [u for u in urls if "/announcements/" in u and ADGM_ENF_KW.search(u) and not ADGM_SKIP_KW.search(u)]
    log("adgm", f"sitemap urls={len(urls)} candidates={len(cand)}")

    for i, url in enumerate(cand):
        slug = url.rstrip("/").rsplit("/", 1)[-1]
        party, raw = _adgm_extract_party(slug)
        details = ""
        amt = ""
        try:
            r = requests.get(url, headers=HDRS, timeout=20)
            if r.status_code == 200:
                soup = BeautifulSoup(r.text, "html.parser")
                title = soup.find("title")
                title_t = title.get_text(strip=True) if title else ""
                # Pull lead paragraph
                main = soup.select_one("main, article, .article-content, .news-content, body")
                text = main.get_text(" ", strip=True)[:3000] if main else ""
                amt = _adgm_extract_amount(title_t + " " + text)
                # Sharper party detection from title
                m = re.search(r"(?:on|fines?|imposes? fines? on|fines)\s+([A-Z][A-Za-z0-9&\.,\- ]+?)(?:\s+for\b|\s+and its\b|\s+USD\b|$)", title_t)
                if m:
                    cand_name = m.group(1).strip().rstrip(".,")
                    if 3 < len(cand_name) < 120:
                        party = cand_name
                details = title_t[:500]
        except Exception:
            pass

        rows.append(base(
            "Abu Dhabi Global Market (ADGM)", "Enforcement Actions",
            party,
            details=details or f"slug: {raw}",
            reward_amount=amt,
            detail_page_url=url,
            link_kind="announcement",
        ))
        time.sleep(0.4)
        if i % 10 == 9:
            log("adgm", f"  ...processed {i+1}/{len(cand)}")

    log("adgm", f"collected {len(rows)} rows")
    return rows


# ============================== FSCA SOUTH AFRICA ============================
FSCA_HEADLINE = re.compile(
    r"(FSCA[\s\-]+(?:imposes|issues|withdraws|debars|suspends|sanctions|penali[sz]es|warns|fines)"
    r".{5,300}|public warning against [A-Z].{2,300}|administrative penalt[a-z]+ on .{2,300}|"
    r"FSCA.{0,30}with an administrative penalty.{2,300})", re.I)


def scrape_fsca():
    rows = []
    seen = set()
    pages = [
        ("https://www.fsca.co.za/Latest-News", "Latest News"),
        ("https://www.fsca.co.za/Enforcement-Matters", "Enforcement Matters"),
        ("https://www.fsca.co.za/Enforcement-Actions", "Enforcement Actions"),
    ]
    for url, label in pages:
        try:
            r = requests.get(url, headers=HDRS, timeout=30)
        except Exception as e:
            log("fsca", f"{label} fetch failed: {e}"); continue
        if r.status_code != 200:
            log("fsca", f"{label} status={r.status_code}")
            continue
        soup = BeautifulSoup(r.text, "html.parser")
        # Find unique headline strings + nearby links
        seen_titles = set()
        for tag in soup.find_all(["h1","h2","h3","h4","h5","p","div","span","a","strong","b"]):
            txt = tag.get_text(" ", strip=True)
            if not txt or len(txt) > 500 or len(txt) < 25:
                continue
            m = FSCA_HEADLINE.search(txt)
            if not m:
                continue
            headline = m.group(0).strip()
            key = re.sub(r"\s+", " ", headline.lower())[:160]
            if key in seen_titles:
                continue
            seen_titles.add(key)

            # Find detail link in same tag or parent
            href = None
            for p in [tag] + list(tag.parents)[:4]:
                if not hasattr(p, "find_all"): continue
                for a in p.find_all("a", href=True):
                    h = a["href"]
                    if any(x in h for x in ["Documents/", ".pdf", ".PDF"]) or "fsca.co.za" in h:
                        href = urljoin(url, h); break
                if href: break

            # Extract entity name from headline
            name = _fsca_extract_name(headline)
            if not name:
                continue
            if name.lower() in seen:
                continue
            seen.add(name.lower())

            amt = ""
            am = re.search(r"R[\s ]*[\d,\.]+(?:[\s ]*(?:million|m|billion))?", headline, re.I)
            if am: amt = am.group(0)

            rows.append(base(
                "Financial Sector Conduct Authority (FSCA South Africa)", "Enforcement Matters",
                name,
                details=headline[:500],
                reward_amount=amt,
                detail_page_url=href or url,
                has_document="yes" if href and ".pdf" in (href or "").lower() else "",
                document_url=href if href and ".pdf" in (href or "").lower() else "",
                link_kind="news_headline",
            ))
    log("fsca", f"collected {len(rows)} unique rows")
    return rows


def _fsca_extract_name(headline):
    """Pull the entity name out of a FSCA enforcement headline."""
    h = headline
    # "FSCA imposes administrative penalt(ies) on <X>"
    m = re.search(r"(?:imposes|issued?|withdraws?|debars?|suspends?|sanctions?|warns?|fines?|"
                  r"penali[sz]es?) (?:administrative penalt(?:y|ies) )?(?:against |on )?"
                  r"([A-Z][A-Za-z0-9&\.,\(\)\- ]+?)(?:[\s,]+(?:with|of|for|amounting|after|under|"
                  r"due to|following)\b|\s*$)", h)
    if m:
        return m.group(1).strip().rstrip(".,-")
    # "public warning against <X>"
    m = re.search(r"public warning against (Mr\.?|Ms\.?|Mrs\.?|the )?([A-Z][A-Za-z0-9&\.,\(\)\- ]+?)(?:\s+for\b|\s*$|,)", h)
    if m:
        return (m.group(1) or "").strip() + " " + m.group(2).strip().rstrip(".,-")
    # "<X> with an administrative penalty"
    m = re.search(r"([A-Z][A-Za-z0-9&\.,\(\)\- ]+?) with an administrative penalt", h)
    if m:
        return m.group(1).strip().rstrip(".,-")
    return ""


# ============================== SEC NIGERIA ==================================
SEC_NG_SECTIONS = [
    # CFEA
    ("https://sec.gov.ng/enforcements/companies-facing-enforcement-action/cfea-april-2015-to-december-2016/", "CFEA Apr2015-Dec2016"),
    ("https://sec.gov.ng/enforcements/companies-facing-enforcement-action/cfea-april-2011-to-march-2015/",   "CFEA Apr2011-Mar2015"),
    ("https://sec.gov.ng/enforcements/companies-facing-enforcement-action/cfea-january-2007-to-march-2011/", "CFEA Jan2007-Mar2011"),
    # APC matters
    ("https://sec.gov.ng/enforcements/apc-matters/apc-decision-wrt-pic-plc-and-others/",      "APC PIC Plc & Others"),
    ("https://sec.gov.ng/enforcements/apc-matters/cases-before-the-apc-0620-0920/",           "APC Jun2020-Sep2020"),
    ("https://sec.gov.ng/enforcements/apc-matters/cases-before-the-apc-0216-0520/",           "APC Feb2016-May2020"),
    ("https://sec.gov.ng/enforcements/apc-matters/cases-before-the-apc-0411-0116/",           "APC Apr2011-Jan2016"),
    ("https://sec.gov.ng/enforcements/apc-matters/cases-before-the-apc-0108-0311/",           "APC Jan2008-Mar2011"),
    # CREA
    ("https://sec.gov.ng/enforcements/referred-cases/crea-0107-0311/",                        "CREA Jan2007-Mar2011"),
    # Litigation
    ("https://sec.gov.ng/enforcements/litigation/recent-litigation-cases-0909-present-day/", "Litigation 2009-Present"),
    ("https://sec.gov.ng/enforcements/litigation/legacy-litigation-cases-0108-0909/",         "Litigation Legacy 2008-2009"),
]


def scrape_sec_ng():
    rows = []
    seen = set()

    # 1) Section pages with tables of cases
    for url, label in SEC_NG_SECTIONS:
        try:
            r = requests.get(url, headers=HDRS, timeout=25)
        except Exception as e:
            log("sec_ng", f"{label} fetch failed: {e}"); continue
        if r.status_code != 200:
            log("sec_ng", f"{label} status={r.status_code}")
            continue
        soup = BeautifulSoup(r.text, "html.parser")
        article = soup.select_one("article, .entry-content, main")
        if not article: continue
        tables = article.find_all("table")
        sec_rows = 0
        for tbl in tables:
            trs = tbl.find_all("tr")
            if len(trs) < 2: continue
            headers = [c.get_text(" ", strip=True).lower() for c in trs[0].find_all(["th","td"])]
            # Map header indices
            idx_name = _find_idx(headers, ["name of company", "name of the company", "name", "company", "respondent", "party"])
            idx_nature = _find_idx(headers, ["nature of enforcement action", "nature of action", "nature", "action", "outcome", "ruling"])
            idx_reason = _find_idx(headers, ["reason", "violation", "matter", "issue"])
            idx_date = _find_idx(headers, ["date"])
            idx_remarks = _find_idx(headers, ["remarks", "status"])

            for tr in trs[1:]:
                cells = [c.get_text(" ", strip=True) for c in tr.find_all(["td","th"])]
                if not cells or all(not c for c in cells): continue
                name = cells[idx_name] if idx_name is not None and idx_name < len(cells) else cells[1] if len(cells)>1 else ""
                name = re.sub(r"^\d+\.?\s*", "", name).strip()
                if not name or len(name) < 2: continue
                key = (name.lower(), label)
                if key in seen: continue
                seen.add(key)

                parts = []
                if idx_nature is not None and idx_nature < len(cells) and cells[idx_nature]:
                    parts.append(f"Action: {cells[idx_nature]}")
                if idx_reason is not None and idx_reason < len(cells) and cells[idx_reason]:
                    parts.append(f"Reason: {cells[idx_reason]}")
                if idx_remarks is not None and idx_remarks < len(cells) and cells[idx_remarks]:
                    parts.append(f"Remarks: {cells[idx_remarks]}")
                details = " | ".join(parts)[:1000]

                date_val = cells[idx_date] if idx_date is not None and idx_date < len(cells) else ""

                rows.append(base(
                    "Securities and Exchange Commission (SEC Nigeria)", "Enforcement Actions",
                    name,
                    case_unit=label,
                    details=details,
                    date_of_birth="",  # date is enforcement date not DOB
                    detail_page_url=url,
                    link_kind="table_row",
                ))
                # Park enforcement date in 'details' (don't misuse DOB)
                if date_val:
                    rows[-1]["details"] = (rows[-1]["details"] + f" | Date: {date_val}")[:1000]
                sec_rows += 1
        log("sec_ng", f"{label}: {sec_rows} rows")
        time.sleep(0.3)

    # 2) Keep Track of Enforcement Updates — list of news items
    upd_url = "https://sec.gov.ng/enforcements/keep-track-of-enforcement-updates/"
    try:
        r = requests.get(upd_url, headers=HDRS, timeout=25)
        soup = BeautifulSoup(r.text, "html.parser")
        article = soup.select_one("article, .entry-content, main")
        if article:
            upd_count = 0
            for a in article.find_all("a", href=True):
                href = urljoin(upd_url, a["href"])
                if "/keep-track-of-enforcement-updates/" not in href.lower(): continue
                if href.rstrip("/").endswith("keep-track-of-enforcement-updates"): continue
                title = a.get_text(" ", strip=True)
                if not title or len(title) < 8: continue
                # Trim trailing date if present
                title_clean = re.sub(r"\s+(Jan\.?|Feb\.?|Mar\.?|Apr\.?|May|Jun\.?|Jul\.?|Aug\.?|Sep\.?|Oct\.?|Nov\.?|Dec\.?)\s+\d{1,2},?\s+\d{4}\b.*$", "", title).strip()
                # Extract entity name from alert-style title
                name = _sec_extract_entity(title_clean)
                if not name: continue
                key = (name.lower(), "keep_track")
                if key in seen: continue
                seen.add(key)
                rows.append(base(
                    "Securities and Exchange Commission (SEC Nigeria)", "Enforcement Actions",
                    name,
                    case_unit="Keep Track of Enforcement Updates",
                    details=title_clean[:500],
                    detail_page_url=href,
                    link_kind="news_item",
                ))
                upd_count += 1
            log("sec_ng", f"keep_track: {upd_count} rows")
    except Exception as e:
        log("sec_ng", f"keep_track fetch failed: {e}")

    log("sec_ng", f"collected {len(rows)} total rows")
    return rows


def _find_idx(headers, needles):
    for i, h in enumerate(headers):
        for n in needles:
            if n in h:
                return i
    return None


def _sec_extract_entity(title):
    t = title.strip()
    # "Illegal Operator Alert - <X>"  /  "Illegal Operator - <X>"
    m = re.search(r"(?:Illegal Operator|Public Notice|Notice Of Cancellation Of Registration Of|Disclaimer On Activity Of|Investors Alert|Scammer Alert|Blacklisting Of)[\s\-:]+[A-Za-z\(\)0-9]+\s*[-–]?\s*(.+)$", t, re.I)
    if m:
        name = m.group(1).strip().rstrip(".,-")
        # Strip date suffix
        name = re.sub(r"\s+(Jan\.?|Feb\.?|Mar\.?|Apr\.?|May|Jun\.?|Jul\.?|Aug\.?|Sep\.?|Oct\.?|Nov\.?|Dec\.?)\s+\d{1,2},?\s+\d{4}\b.*$", "", name).strip()
        return name if 2 < len(name) < 200 else ""
    # Generic: trim known prefixes
    for pref in ["Illegal Operator Alert", "Illegal Operator", "Public Notice", "Notice Of", "Scammer Alert", "Disclaimer", "Activities Of", "Blacklisting Of", "Ponzi:", "Investors Alert"]:
        if t.lower().startswith(pref.lower()):
            rest = t[len(pref):].strip(" -–:").strip()
            if 3 < len(rest) < 200: return rest
    return t if 3 < len(t) < 200 else ""


# ============================== ORCHESTRATOR ==================================
def main():
    log("phase2", "=== START Thursday 2026-05-22 ME/Africa Phase-2 scraping ===")
    out = {}

    # Working sources
    for name, fn, csv_name in [
        ("adgm",   scrape_adgm,   "adgm_enforcement.csv"),
        ("fsca",   scrape_fsca,   "fsca_enforcement.csv"),
        ("sec_ng", scrape_sec_ng, "sec_nigeria_enforcement.csv"),
    ]:
        try:
            rows = fn()
        except Exception as e:
            log(name, f"FATAL: {e}")
            rows = []
        if rows:
            path, n = write_csv(rows, csv_name)
            log(name, f"wrote {n} rows -> {path}")
            out[name] = n
        else:
            out[name] = 0

    # Blocked sources — write empty placeholder CSVs so registration is consistent
    for name, agency, lst, csv_name, reason in [
        ("cma_saudi", "Capital Market Authority Saudi Arabia (CMA)", "Enforcement Decisions",
         "cma_saudi_enforcement.csv", "SharePoint SPA — JS-rendered content"),
        ("sama",      "Saudi Arabian Monetary Authority (SAMA)", "Enforcement Actions",
         "sama_enforcement.csv",       "SharePoint SPA — enforcement page is shell only"),
        ("efcc_ng",   "Economic and Financial Crimes Commission (EFCC Nigeria)", "Conviction Records",
         "efcc_nigeria_convictions.csv","Cloudflare 403 challenge on every URL"),
    ]:
        log(name, f"BLOCKED: {reason}")
        # Write an empty CSV with just the header (for parity)
        path = os.path.join(DATA, csv_name)
        with open(path, "w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=HEADER); w.writeheader()
        out[name] = 0

    log("phase2", f"=== DONE: {out} ===")
    return out


if __name__ == "__main__":
    main()
