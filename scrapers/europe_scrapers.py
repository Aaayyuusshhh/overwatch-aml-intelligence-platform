#!/usr/bin/env python3
"""
European regulator scrapers — bulk international expansion.

Sources covered:
  - CSSF Luxembourg public warnings (cssf_luxembourg_warnings)  [NEW]
  - CONSOB Italy sanctions (consob_italy_sanctions)             [NEW]
  - FI Sweden sanctions register (fi_sweden_sanctions)          [NEW]
  - CBI Ireland enforcement press releases (cbi_ireland_enf)    [NEW]
  - Finanstilsynet Norway warnings (finanstilsynet_no_warnings) [NEW]

Each function returns a list of dicts in the standard 17-col schema and the
__main__ entry-point writes one CSV per source.
"""
from __future__ import annotations
import csv
import re
import time
import warnings
from datetime import datetime
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

warnings.filterwarnings("ignore")

H = {
    "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}

FIELDS = [
    "source_agency", "source_list", "case_unit", "name", "father_name",
    "date_of_birth", "gender", "address", "reward_amount", "details",
    "has_document", "document_url", "detail_page_url", "interpol_notice_id",
    "link_kind", "scraped_at", "enrichment_status",
]


def _row(agency: str, list_name: str, name: str, **kw) -> dict:
    base = {f: "" for f in FIELDS}
    base["source_agency"] = agency
    base["source_list"] = list_name
    base["name"] = (name or "").strip()
    base["scraped_at"] = datetime.utcnow().isoformat()[:19]
    for k, v in kw.items():
        if k in FIELDS and v is not None:
            base[k] = str(v).strip() if not isinstance(v, str) else v.strip()
    return base


def _write_csv(rows: list[dict], path: str) -> int:
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS)
        w.writeheader()
        for r in rows:
            w.writerow({k: r.get(k, "") for k in FIELDS})
    print(f"  wrote {len(rows):>5d} rows -> {path}")
    return len(rows)


# --------------------------------------------------------------------------
# 1. CSSF Luxembourg — Public Warnings
# --------------------------------------------------------------------------
def scrape_cssf_warnings() -> list[dict]:
    """
    The CSSF publishes 10 warnings per page across ~38 pages.
    Each "warning" entry is a link with title like
       "Warning concerning the website XYZ"
    The slug after the date encodes the name(s).
    """
    AG, LN = "Luxembourg CSSF", "Public Warnings"
    rows: list[dict] = []
    seen = set()

    # discover last page via "Last page" link
    r = requests.get("https://www.cssf.lu/en/warnings/", headers=H, timeout=30, verify=False)
    soup = BeautifulSoup(r.text, "html.parser")
    last = 1
    for a in soup.find_all("a", href=re.compile(r"/en/warnings/page/(\d+)/")):
        m = re.search(r"/page/(\d+)/", a["href"])
        if m:
            last = max(last, int(m.group(1)))
    print(f"  CSSF: discovered {last} pages")

    for page in range(1, last + 1):
        url = "https://www.cssf.lu/en/warnings/" if page == 1 else f"https://www.cssf.lu/en/warnings/page/{page}/"
        try:
            r = requests.get(url, headers=H, timeout=30, verify=False)
            soup = BeautifulSoup(r.text, "html.parser")
        except Exception as e:
            print(f"    page {page}: ERR {e}")
            continue

        # warning links live under article / main; only EN entries
        link_re = re.compile(r"/en/\d{4}/\d{2}/")
        items = soup.find_all("a", href=link_re)
        for a in items:
            href = urljoin(url, a["href"])
            title = a.get_text(strip=True)
            if not title or title.lower() == "page" or href in seen:
                continue
            seen.add(href)

            # extract date from URL
            m = re.search(r"/(\d{4})/(\d{2})/", href)
            published = f"{m.group(1)}-{m.group(2)}" if m else ""

            # Subject extraction: titles like "Warning concerning the website XYZ"
            # or "Warning regarding ABC"
            subject = title
            for prefix in ["Warning concerning the website ", "Warning concerning ",
                           "Warning regarding ", "Warning about ", "Warning – ",
                           "Warning related to "]:
                if title.lower().startswith(prefix.lower()):
                    subject = title[len(prefix):]
                    break

            rows.append(_row(
                AG, LN,
                subject.strip().rstrip("."),
                case_unit=f"CSSF Warning {published}",
                details=title,
                detail_page_url=href,
                link_kind="warning",
            ))

        print(f"    page {page:>2d} -> running total {len(rows)}")
        time.sleep(0.4)
    return rows


# --------------------------------------------------------------------------
# 2. CONSOB Italy — Sanctions
# --------------------------------------------------------------------------
def scrape_consob_sanctions() -> list[dict]:
    """
    CONSOB sanctions page enumerates 'Delibera n. XXXX' entries each with
    a dettaglio-sanzione asset URL. Names are inside the sanction text — we
    extract the delibera number + the descriptive subject line.
    """
    AG = "Commissione Nazionale per le Società e la Borsa (CONSOB)"
    LN = "Sanctions"
    rows: list[dict] = []
    seen = set()

    base = "https://www.consob.it/web/area-pubblica/sanzioni"
    # Most CONSOB listings paginate via _p_r_p_resetCur=false&_<id>_keywords or simple page params
    # Try sequential ?page= and also follow Liferay-style asset pagination via _delta=
    for page in range(1, 25):
        if page == 1:
            url = base
        else:
            url = f"{base}?_p_r_p_curPage={page}"
        try:
            r = requests.get(url, headers=H, timeout=30, verify=False)
            soup = BeautifulSoup(r.text, "html.parser")
        except Exception as e:
            print(f"    consob page {page}: ERR {e}")
            break

        items = soup.find_all("a", href=re.compile(r"dettaglio-sanzione"))
        if not items:
            print(f"    consob page {page}: no items, stopping")
            break

        new_on_page = 0
        for a in items:
            href = urljoin(url, a["href"])
            title = a.get_text(strip=True)
            if not title or href in seen:
                continue
            seen.add(href)
            new_on_page += 1

            # Parse 'Delibera n. XXXXX, …'
            m = re.match(r"Delibera n\.\s*(\d+)[^,]*,\s*(.+)", title, re.I)
            delibera = m.group(1) if m else ""
            subject = m.group(2).strip() if m else title

            # try to surface a target name from common CONSOB italian patterns
            # e.g. "applicazione di sanzioni amministrative nei confronti di XYZ"
            tm = re.search(r"nei confronti di\s+([^,\.;]+)", subject, re.I)
            target = tm.group(1).strip() if tm else subject[:120]

            rows.append(_row(
                AG, LN, target,
                case_unit=f"Delibera n. {delibera}" if delibera else "Sanction",
                details=title,
                detail_page_url=href,
                link_kind="sanction",
            ))

        print(f"    consob page {page:>2d}: +{new_on_page} new -> {len(rows)} total")
        if new_on_page == 0:
            break
        time.sleep(0.4)

    return rows


# --------------------------------------------------------------------------
# 3. FI Sweden — Sanctions (Finansinspektionen)
# --------------------------------------------------------------------------
def scrape_fi_sweden_sanctions() -> list[dict]:
    """
    Finansinspektionen lists sanctions under /en/published/sanctions/
    and subpages /financial-firms/ and /financial-reporting-supervision/.
    """
    AG = "Finansinspektionen (FI Sweden)"
    LN = "Sanctions"
    rows: list[dict] = []
    seen = set()

    subpages = [
        "https://www.fi.se/en/published/sanctions/financial-firms/",
        "https://www.fi.se/en/published/sanctions/financial-reporting-supervision/",
    ]

    for sub in subpages:
        # paginate ?page= until empty
        for page in range(1, 60):
            sep = "&" if "?" in sub else "?"
            url = sub if page == 1 else f"{sub}{sep}page={page}"
            try:
                r = requests.get(url, headers=H, timeout=30, verify=False)
                soup = BeautifulSoup(r.text, "html.parser")
            except Exception as e:
                print(f"    fi.se {sub} page {page}: ERR {e}")
                break

            # FI uses list of result items; titles are usually <a> with /en/published/
            items = soup.select('a[href*="/en/published/sanctions/"]')
            entries = []
            for a in items:
                href = urljoin(url, a["href"])
                if href in seen or href.rstrip("/").endswith("sanctions"):
                    continue
                txt = a.get_text(strip=True)
                if not txt or len(txt) < 4:
                    continue
                # skip nav items
                if txt.lower() in ("financial firms", "financial reporting supervision",
                                   "sanctions", "next", "previous"):
                    continue
                seen.add(href)
                entries.append((txt, href))

            for title, href in entries:
                rows.append(_row(
                    AG, LN, title[:200],
                    case_unit="FI Sanction",
                    details=title,
                    detail_page_url=href,
                    link_kind="sanction",
                ))

            if not entries:
                print(f"    fi.se {sub.split('/')[-2]} page {page}: empty, stopping")
                break
            print(f"    fi.se {sub.split('/')[-2]} page {page}: +{len(entries)} -> {len(rows)}")
            time.sleep(0.4)
    return rows


# --------------------------------------------------------------------------
# 4. CBI Ireland — Enforcement press releases
# --------------------------------------------------------------------------
def scrape_cbi_ireland_enforcement() -> list[dict]:
    """
    Central Bank of Ireland press releases filtered by Enforcement topic.
    """
    AG = "Central Bank of Ireland (CBI)"
    LN = "Enforcement Press Releases"
    rows: list[dict] = []
    seen = set()

    base = "https://www.centralbank.ie/news-media/press-releases"
    for page in range(1, 60):
        params = "?topic=Enforcement"
        url = f"{base}{params}" if page == 1 else f"{base}/page-{page}{params}"
        try:
            r = requests.get(url, headers=H, timeout=30, verify=False)
            soup = BeautifulSoup(r.text, "html.parser")
        except Exception as e:
            print(f"    cbi.ie page {page}: ERR {e}")
            break

        items = soup.select('a[href*="/news/article/"], a[href*="/news/press-releases/"], a.search-results__title, h2.search-results__title a, li.listing a')
        # generic: any /news/ link
        if not items:
            items = soup.select('a[href*="/news/"]')

        new_count = 0
        for a in items:
            href = urljoin(url, a.get("href", ""))
            title = a.get_text(" ", strip=True)
            if not title or href in seen or "/news/" not in href:
                continue
            if len(title) < 10:
                continue
            # filter: must look like a press release link
            if not re.search(r"(enforce|sanction|fine|penalt|reprimand|suspen|revoke|disqualif|disciplin|settle)", title, re.I):
                continue
            seen.add(href)
            new_count += 1
            # try extract target name from "Central Bank of Ireland fines XYZ"
            m = re.search(r"(?:fines?|reprimands?|sanctions?|penalises?|disqualifies?|prohibits?)\s+(.+?)(?:\s+€|\s+€|\s+\$|\s+for\b|\s+over\b|\s+in\s+respect|\s*$)", title, re.I)
            target = m.group(1).strip() if m else title[:120]
            rows.append(_row(
                AG, LN, target,
                case_unit="CBI Enforcement",
                details=title,
                detail_page_url=href,
                link_kind="enforcement",
            ))

        if new_count == 0:
            print(f"    cbi.ie page {page}: 0 matches, stopping")
            break
        print(f"    cbi.ie page {page}: +{new_count} -> {len(rows)}")
        time.sleep(0.4)
    return rows


# --------------------------------------------------------------------------
# 5. Finanstilsynet Norway — Warnings
# --------------------------------------------------------------------------
def scrape_finanstilsynet_norway() -> list[dict]:
    """
    FSA Norway warnings list (small but real source).
    """
    AG = "Finanstilsynet (Norway)"
    LN = "Warnings"
    rows: list[dict] = []
    seen = set()

    candidates = [
        "https://www.finanstilsynet.no/en/news-archive/news/?categories=Warnings",
        "https://www.finanstilsynet.no/en/news-archive/news/?type=Warning",
        "https://www.finanstilsynet.no/en/news-archive/news/",
        "https://www.finanstilsynet.no/en/news-archive/warnings/",
    ]
    for url in candidates:
        try:
            r = requests.get(url, headers=H, timeout=30, verify=False)
            if r.status_code != 200:
                continue
            soup = BeautifulSoup(r.text, "html.parser")
        except Exception:
            continue
        items = soup.select('a[href*="/news/"], a[href*="/warning"]')
        for a in items:
            href = urljoin(url, a.get("href", ""))
            title = a.get_text(" ", strip=True)
            if not title or href in seen:
                continue
            if not re.search(r"warning|misuse|fraud|alert", title, re.I):
                continue
            seen.add(href)
            rows.append(_row(
                AG, LN, title[:200],
                case_unit="FSA Warning",
                details=title,
                detail_page_url=href,
                link_kind="warning",
            ))
    return rows


# --------------------------------------------------------------------------
if __name__ == "__main__":
    import os, sys
    os.makedirs("data", exist_ok=True)

    targets = [
        ("cssf_luxembourg_warnings", scrape_cssf_warnings),
        ("consob_italy_sanctions",   scrape_consob_sanctions),
        ("fi_sweden_sanctions",      scrape_fi_sweden_sanctions),
        ("cbi_ireland_enforcement",  scrape_cbi_ireland_enforcement),
        ("finanstilsynet_no_warnings", scrape_finanstilsynet_norway),
    ]

    only = sys.argv[1:] if len(sys.argv) > 1 else None
    for sid, fn in targets:
        if only and sid not in only:
            continue
        print(f"\n=== {sid} ===")
        t0 = time.time()
        try:
            rows = fn()
            _write_csv(rows, f"data/{sid}.csv")
            print(f"  done in {time.time()-t0:.1f}s")
        except Exception as e:
            print(f"  FAILED: {type(e).__name__}: {e}")
