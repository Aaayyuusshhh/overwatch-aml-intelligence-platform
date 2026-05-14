"""
engines/recon_engine.py — automated reconnaissance for AML-relevant
content on a given domain.

For each domain we:
  1. Fetch homepage + sitemap.xml + robots.txt.
  2. Crawl up to 100 pages two-levels-deep within the same domain.
  3. On every page, look for AML keywords in the visible text, count
     <table> tags with ≥3 rows, and extract downloadable-file links.
  4. Detect the framework powering the site.
  5. Score each page on AML relevance (0-100) and return the top hits.

The output is a structured JSON report (see Output schema below).

CLI:
  python engines/recon_engine.py --domain example.gov.in
  python engines/recon_engine.py --batch configs/domains.txt --output reports/foo.json
  python engines/recon_engine.py --domain example.gov.in --verbose

This is a *discovery* tool — it does not scrape data. The output
guides which pages a human (or a custom scraper) should target next.
"""

import argparse
import json
import os
import re
import sys
import time
from collections import OrderedDict
from urllib.parse import urljoin, urlparse, urldefrag

import requests
import warnings
warnings.filterwarnings("ignore")

try:
    from bs4 import BeautifulSoup
except Exception:
    BeautifulSoup = None

UA = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                    "Chrome/120.0.0.0 Safari/537.36",
      "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
      "Accept-Language": "en-US,en;q=0.9"}

REQUEST_TIMEOUT = 15
REQUEST_DELAY   = 2.0
MAX_PAGES       = 100

AML_KEYWORDS = [
    "wanted", "most wanted", "absconding", "proclaimed", "fugitive",
    "criminal", "offender", "defaulter", "debarred", "blacklist", "banned",
    "suspended", "penalty", "enforcement", "cancelled", "struck off",
    "wilful", "fraud", "corruption", "vigilance", "arrested",
]

# Common AML-list URL slugs to probe directly even if the homepage
# doesn't link to them (handles SPA navs, hidden pages).
COMMON_AML_PATHS = [
    "/wanted", "/wanted-person", "/wanted-persons",
    "/most-wanted", "/wanted-criminals",
    "/absconders", "/absconding-criminals", "/absconder-list",
    "/proclaimed-offenders", "/proclaimed-offender",
    "/fugitive", "/fugitives",
    "/defaulter", "/defaulters", "/defaulters-list", "/wilful-defaulters",
    "/debarred", "/debarred-list", "/debarred-vendors",
    "/blacklist", "/blacklisted", "/blacklisted-vendors", "/blacklisted-firms",
    "/banned", "/banned-firms", "/banning-list",
    "/suspended", "/suspended-members",
    "/penalty", "/penalties",
    "/enforcement", "/enforcement-orders", "/enforcement-action",
    "/notice", "/notices", "/circulars",
    "/vigilance", "/vigilance-orders",
    "/struck-off",
    "/regulatory-orders", "/regulatory-actions",
    # Insolvency / corporate-action variants
    "/public-announcement", "/public-announcements",
    "/insolvency-resolution", "/cirp", "/liquidation",
    # Tax / financial-crime variants
    "/tax-defaulters", "/economic-offenders", "/wilful-defaulter",
    # PSU / vendor-blacklist variants
    "/vendor-blacklist", "/vendor-debarment", "/debarment-list",
    # English-locale prefixed variants used by some Indian gov sites
    "/en/wanted", "/en/wanted-person", "/en/defaulters", "/en/notice",
    "/en/public-announcement",
]

DOWNLOAD_EXT = (".pdf", ".xlsx", ".xls", ".csv", ".zip", ".xml",
                ".doc", ".docx")
ASSET_EXT    = (".png", ".jpg", ".jpeg", ".gif", ".svg", ".css", ".js",
                ".woff", ".woff2", ".ttf", ".ico", ".mp4", ".mp3", ".eot")

FRAMEWORK_SIGS = {
    "Angular":  ("ng-app", "ng-version", "angular"),
    "React":    ("react", "__NEXT", "_next"),
    "Drupal":   ("drupal", "/jsonapi", "/node/"),
    "WordPress":("wp-content", "wp-json"),
    "Liferay":  ("/o/headless", "liferay"),
    "ASP.NET":  ("__VIEWSTATE", ".aspx"),
    "Umbraco":  ("/umbraco",),
}


# ---------- helpers --------------------------------------------------------
def _vprint(verbose, *args, **kwargs):
    if verbose:
        print(*args, **kwargs, flush=True)


def _normalise_url(u):
    """Strip fragments, normalise scheme/host."""
    u, _ = urldefrag(u)
    return u.rstrip("/")


def _same_domain(url, base_netloc):
    try:
        host = urlparse(url).netloc.lower()
    except Exception:
        return False
    if not host:
        return False
    if host == base_netloc:
        return True
    # Allow www. prefix variants
    return host.lstrip("www.") == base_netloc.lstrip("www.")


def _asset_url(url):
    p = urlparse(url).path.lower()
    return p.endswith(ASSET_EXT)


def _has_download_ext(url):
    p = urlparse(url).path.lower()
    return p.endswith(DOWNLOAD_EXT)


def _safe_get(url):
    try:
        r = requests.get(url, headers=UA, timeout=REQUEST_TIMEOUT,
                          verify=False, allow_redirects=True)
        return r
    except Exception as e:
        return e


def _framework(html):
    low = html.lower()
    for name, sigs in FRAMEWORK_SIGS.items():
        if any(sig.lower() in low for sig in sigs):
            return name
    return "unknown"


def _visible_text(html):
    if BeautifulSoup is None:
        return re.sub(r"<[^>]+>", " ", html)
    soup = BeautifulSoup(html, "html.parser")
    for s in soup(["script", "style", "noscript", "nav", "header", "footer"]):
        s.decompose()
    return soup.get_text(" ", strip=True)


def _page_title(html):
    m = re.search(r"<title[^>]*>([^<]{1,200})</title>", html, re.I)
    return (m.group(1).strip() if m else "")[:200]


def _page_headings(html):
    """Return the concatenated text of <h1>/<h2>/<h3> tags."""
    if BeautifulSoup is None:
        return ""
    soup = BeautifulSoup(html, "html.parser")
    parts = []
    for tag in soup.find_all(["h1", "h2", "h3"]):
        parts.append(tag.get_text(" ", strip=True))
    return " | ".join(parts)[:1000]


def _table_row_count(html):
    """Largest <table> by row count on the page."""
    if BeautifulSoup is None:
        return 0
    soup = BeautifulSoup(html, "html.parser")
    counts = [len(t.find_all("tr")) for t in soup.find_all("table")]
    return max(counts) if counts else 0


def _internal_links(html, base_url, base_netloc):
    """Return list of absolute, same-domain, non-asset URLs found on
    the page."""
    out = []
    if BeautifulSoup is None:
        candidates = re.findall(r'href=["\']([^"\'#]+)["\']', html, re.I)
    else:
        soup = BeautifulSoup(html, "html.parser")
        candidates = [a.get("href", "") for a in soup.find_all("a", href=True)]
    seen = set()
    for href in candidates:
        if not href or href.startswith(("mailto:", "tel:", "javascript:")):
            continue
        absu = _normalise_url(urljoin(base_url, href))
        if absu in seen:
            continue
        seen.add(absu)
        if not absu.startswith(("http://", "https://")):
            continue
        if not _same_domain(absu, base_netloc):
            continue
        if _asset_url(absu):
            continue
        out.append(absu)
    return out


def _aml_hits(text):
    """Return list of unique AML keywords found in `text`."""
    low = text.lower()
    return sorted({kw for kw in AML_KEYWORDS if kw in low})


def _aml_in_url(url):
    low = url.lower()
    return any(kw.replace(" ", "-") in low or kw.replace(" ", "_") in low
               or kw in low for kw in AML_KEYWORDS)


def _downloads_on_page(html, base_url):
    if BeautifulSoup is None:
        hrefs = re.findall(r'href=["\']([^"\']+)["\']', html, re.I)
    else:
        soup = BeautifulSoup(html, "html.parser")
        hrefs = [a.get("href", "") for a in soup.find_all("a", href=True)]
    out = []
    seen = set()
    for h in hrefs:
        absu = _normalise_url(urljoin(base_url, h))
        if not _has_download_ext(absu):
            continue
        if absu in seen:
            continue
        seen.add(absu)
        out.append(absu)
    return out


def _score(strong_kws, weak_kws, table_rows, downloads, url, page_size):
    """Score 0-100. Strong keywords (URL/title/heading) score 30; weak
    body-only mentions score 10 to discount nav-menu noise."""
    s = 0
    if strong_kws:
        s += 30
    elif weak_kws:
        s += 10
    if table_rows >= 3:
        s += 30
    if downloads:
        s += 20
    if _aml_in_url(url):
        s += 10
    if page_size >= 10_000:
        s += 10
    return min(s, 100)


def _sitemap_aml_urls(domain):
    """Try sitemap.xml; return list of URLs whose path contains an AML
    keyword."""
    for url in (f"https://{domain}/sitemap.xml",
                f"https://www.{domain}/sitemap.xml"):
        r = _safe_get(url)
        if isinstance(r, Exception) or r.status_code != 200:
            continue
        urls = re.findall(r"<loc>([^<]+)</loc>", r.text)
        return sorted({u for u in urls if _aml_in_url(u)})
    return []


def _robots_disallows(domain):
    for url in (f"https://{domain}/robots.txt",
                f"https://www.{domain}/robots.txt"):
        r = _safe_get(url)
        if isinstance(r, Exception) or r.status_code != 200:
            continue
        out = []
        for line in r.text.splitlines():
            line = line.strip()
            if line.lower().startswith("disallow:"):
                path = line.split(":", 1)[1].strip()
                if path and path != "/":
                    out.append(path)
        return sorted(set(out))
    return []


# ---------- main per-domain ------------------------------------------------
def recon(domain, max_pages=MAX_PAGES, verbose=False):
    # lstrip("https://") would strip *characters* (h, t, p, s, :, /) from
    # the start — so "police.rajasthan.gov.in" loses its leading 'p'.
    # Use removeprefix instead.
    domain = domain.strip().lower().rstrip("/")
    for pfx in ("https://", "http://"):
        if domain.startswith(pfx):
            domain = domain[len(pfx):]
            break
    base_url = f"https://{domain}/"
    parsed = urlparse(base_url)
    base_netloc = parsed.netloc

    report = OrderedDict([
        ("domain", domain),
        ("status", "success"),
        ("framework", "unknown"),
        ("pages_crawled", 0),
        ("total_links_found", 0),
        ("discoveries", []),
        ("all_downloadable_files", []),
        ("sitemap_urls_with_keywords", []),
        ("robots_disallowed_paths", []),
        ("error", None),
    ])

    _vprint(verbose, f"[{domain}] homepage fetch ...")
    r = _safe_get(base_url)
    if isinstance(r, Exception):
        report["status"] = "unreachable"
        report["error"] = f"{type(r).__name__}: {str(r)[:160]}"
        _vprint(verbose, f"  unreachable: {report['error']}")
        return report
    if r.status_code >= 400:
        report["status"] = "unreachable"
        report["error"] = f"homepage status {r.status_code}"
        _vprint(verbose, f"  {report['error']}")
        return report

    homepage_html = r.text
    report["framework"] = _framework(homepage_html)
    _vprint(verbose, f"  framework={report['framework']}  "
                       f"len={len(homepage_html)}")

    # ---- BFS crawl ----
    base_netloc_norm = base_netloc.lower()
    visited = set()
    queue = [(base_url, 0)]
    page_results = []
    all_downloads = {}                # url -> {url,filename}

    homepage_links = _internal_links(homepage_html, base_url, base_netloc_norm)
    report["total_links_found"] = len(homepage_links)

    def _record_page(url, html, depth):
        # Body text excludes <nav>/<header>/<footer> so nav-menu words
        # don't trigger keyword hits on every page.
        text = _visible_text(html)[:80_000]
        title = _page_title(html)
        headings = _page_headings(html)
        # Strong signal: keyword in URL path, title, or H1/H2/H3.
        strong_text = (urlparse(url).path + " " + title + " " + headings).lower()
        strong_kws = sorted({kw for kw in AML_KEYWORDS if kw in strong_text})
        # Weak signal: keyword in body (post-nav-strip).
        weak_kws = sorted({kw for kw in AML_KEYWORDS
                            if kw in text.lower() and kw not in strong_kws})
        rows = _table_row_count(html)
        dls  = _downloads_on_page(html, url)
        for dl in dls:
            all_downloads.setdefault(dl, {
                "url": dl,
                "filename": dl.rsplit("/", 1)[-1].split("?")[0],
            })
        score = _score(strong_kws, weak_kws, rows, dls, url, len(html))
        page_results.append({
            "url": url,
            "title": title,
            "score": score,
            "keywords_found": sorted(set(strong_kws + weak_kws)),
            "strong_keywords": strong_kws,
            "has_table": rows >= 3,
            "table_row_count": rows,
            "downloadable_files": [d.rsplit("/", 1)[-1].split("?")[0] for d in dls],
            "page_size_kb": round(len(html) / 1024, 1),
            "depth": depth,
        })

    # Record homepage itself
    visited.add(base_url)
    _record_page(base_url, homepage_html, depth=0)

    # depth-1 queue
    for link in homepage_links:
        if link not in visited:
            queue.append((link, 1))

    while queue and len(visited) < max_pages:
        url, depth = queue.pop(0)
        if url in visited:
            continue
        if depth > 2:
            continue
        _vprint(verbose, f"  [{depth}] GET {url[:120]}")
        r = _safe_get(url)
        time.sleep(REQUEST_DELAY)
        visited.add(url)
        if isinstance(r, Exception):
            _vprint(verbose, f"      ERR {type(r).__name__}: {str(r)[:100]}")
            continue
        if r.status_code >= 400:
            _vprint(verbose, f"      {r.status_code}")
            continue
        ct = r.headers.get("content-type", "").lower()
        if "html" not in ct and "xml" not in ct:
            continue
        html = r.text
        _record_page(url, html, depth)
        if depth < 2:
            for next_link in _internal_links(html, url, base_netloc_norm):
                if next_link not in visited and len(visited) + len(queue) < max_pages * 3:
                    queue.append((next_link, depth + 1))

    report["pages_crawled"] = len(page_results)

    # Sort discoveries by score; drop pages with score 0
    page_results.sort(key=lambda d: (-d["score"], d["url"]))
    report["discoveries"] = [p for p in page_results if p["score"] > 0]
    report["all_downloadable_files"] = list(all_downloads.values())

    # Sitemap + robots
    _vprint(verbose, f"[{domain}] sitemap + robots ...")
    report["sitemap_urls_with_keywords"] = _sitemap_aml_urls(domain)
    report["robots_disallowed_paths"]    = _robots_disallows(domain)

    # Direct probe of common AML paths (catches SPA-only nav links).
    _vprint(verbose, f"[{domain}] probing {len(COMMON_AML_PATHS)} common AML paths ...")
    probed = 0
    for path in COMMON_AML_PATHS:
        if len(visited) >= max_pages * 2:
            break
        url = f"https://{domain}{path}"
        if url in visited:
            continue
        # one retry — slow gov sites often time out under burst.
        rsp = _safe_get(url)
        if isinstance(rsp, Exception):
            time.sleep(1.0)
            rsp = _safe_get(url)
        time.sleep(REQUEST_DELAY / 2)
        if isinstance(rsp, Exception):
            _vprint(verbose, f"  · {path}: {type(rsp).__name__}")
            continue
        if rsp.status_code != 200 or len(rsp.content) < 1500:
            continue
        ct = rsp.headers.get("content-type", "").lower()
        if "html" not in ct:
            continue
        visited.add(url)
        probed += 1
        _record_page(url, rsp.text, depth=0)
        _vprint(verbose, f"  ✓ {path} (len={len(rsp.content)})")
    if probed:
        # Re-sort discoveries after probing
        page_results.sort(key=lambda d: (-d["score"], d["url"]))
        report["discoveries"] = [p for p in page_results if p["score"] > 0]
        report["pages_crawled"] = len(page_results)

    _vprint(verbose, f"[{domain}] done. pages={report['pages_crawled']}  "
                       f"discoveries={len(report['discoveries'])}  "
                       f"files={len(report['all_downloadable_files'])}")
    return report


# ---------- CLI ------------------------------------------------------------
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--domain", help="single domain (e.g. example.gov.in)")
    ap.add_argument("--batch",  help="file with one domain per line")
    ap.add_argument("--output", help="write JSON report to this file")
    ap.add_argument("--max-pages", type=int, default=MAX_PAGES,
                    help=f"max pages per domain (default {MAX_PAGES})")
    ap.add_argument("--verbose", action="store_true")
    args = ap.parse_args()

    domains = []
    if args.batch:
        with open(args.batch, "r", encoding="utf-8") as f:
            domains = [ln.strip() for ln in f
                       if ln.strip() and not ln.startswith("#")]
    if args.domain:
        domains.append(args.domain.strip())
    if not domains:
        ap.error("provide --domain or --batch")

    results = []
    for i, dom in enumerate(domains, start=1):
        print(f"\n========== [{i}/{len(domains)}] {dom} ==========", flush=True)
        try:
            r = recon(dom, max_pages=args.max_pages, verbose=args.verbose)
        except Exception as e:
            r = {"domain": dom, "status": "error",
                 "error": f"{type(e).__name__}: {e}"}
        results.append(r)
        n_disc = len(r.get("discoveries", []))
        n_files = len(r.get("all_downloadable_files", []))
        print(f"  -> status={r.get('status')}  framework={r.get('framework')}  "
              f"pages={r.get('pages_crawled',0)}  discoveries={n_disc}  "
              f"files={n_files}", flush=True)
        if r.get("discoveries"):
            top = r["discoveries"][0]
            print(f"  top: score={top['score']}  {top['url'][:120]}", flush=True)

    payload = results if len(results) > 1 else results[0]
    if args.output:
        os.makedirs(os.path.dirname(args.output) or ".", exist_ok=True)
        with open(args.output, "w", encoding="utf-8") as f:
            json.dump(payload, f, indent=2)
        print(f"\nReport saved: {args.output}", flush=True)
    else:
        print(json.dumps(payload, indent=2))


if __name__ == "__main__":
    main()
