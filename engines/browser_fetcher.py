"""
Tier 2 fetch: full browser rendering via Scrapling's StealthyFetcher
(Playwright/Chromium under the hood).

Returns a parsed Scrapling Adaptor page that exposes the same
.find_all / .body / .html_content interface as a Tier-1 Fetcher.get
response, so the existing extraction strategies in html_scraper run
unchanged.

If StealthyFetcher (or its Playwright dependency) cannot be imported,
fetch_rendered() returns (None, None, 0.0) and a warning is logged
once - the caller falls back to static fetch.
"""

import re
import time

# Guarded import - the rest of the pipeline must keep working even if
# Playwright browsers aren't installed.
try:
    from scrapling import StealthyFetcher
    _STEALTHY_AVAILABLE = True
    _STEALTHY_ERR = None
except Exception as e:                      # pragma: no cover
    _STEALTHY_AVAILABLE = False
    _STEALTHY_ERR = f"{type(e).__name__}: {e}"

POLITENESS_SECONDS = 2.0
DEFAULT_TIMEOUT_MS = 45_000     # 45 s per spec
DEFAULT_WAIT_MS    = 3_000

# Markers that suggest a page's data is loaded by JavaScript and the
# static fetch we already performed therefore got an empty shell.
JS_INDICATORS = (
    "loading...",
    "please wait",
    "ng-app=",
    "ng-version=",
    "data-reactroot",
    "data-reactid",
    "__next_data__",
    "react-dom",
    "<noscript",
    'id="root"', "id='root'",
    'id="app"',  "id='app'",
)

_warned = False
_last_fetch_at = 0.0


def is_js_indicated(page_or_html):
    """Return True if the static page body contains JS-render markers
    suggesting the actual data hasn't loaded yet. Accepts either a
    Scrapling page or a raw html string."""
    if page_or_html is None:
        return False
    if isinstance(page_or_html, (str, bytes)):
        body = page_or_html
    else:
        body = (getattr(page_or_html, "html_content", None)
                or getattr(page_or_html, "body", None) or "")
    if isinstance(body, bytes):
        body = body.decode("utf-8", "replace")
    if not body:
        return False
    body_lc = body.lower()
    # Empty <tbody> alongside a populated <table> wrapper - very common
    # when the table is filled by AJAX after page load.
    if "<table" in body_lc and re.search(r"<tbody[^>]*>\s*</tbody>", body_lc):
        return True
    return any(m in body_lc for m in JS_INDICATORS)


def _polite_sleep():
    global _last_fetch_at
    elapsed = time.time() - _last_fetch_at
    if elapsed < POLITENESS_SECONDS:
        time.sleep(POLITENESS_SECONDS - elapsed)


def fetch_rendered(url, wait_seconds=3, timeout=45):
    """Tier-2 fetch. Returns (page_obj, status_code, fetch_time_seconds).

    page_obj is a Scrapling Adaptor with the same interface as a
    Fetcher.get response, so the existing extraction strategies in
    html_scraper.py work without changes. On any error, returns
    (None, None, elapsed)."""
    global _warned, _last_fetch_at
    if not _STEALTHY_AVAILABLE:
        if not _warned:
            print(f"WARN browser_fetcher: StealthyFetcher unavailable "
                  f"({_STEALTHY_ERR}); Tier-2 disabled, falling back to static")
            _warned = True
        return None, None, 0.0

    _polite_sleep()
    t0 = time.time()
    try:
        page = StealthyFetcher.fetch(
            url,
            headless=True,
            network_idle=True,
            timeout=timeout * 1000,
            wait=int(wait_seconds * 1000) if wait_seconds else DEFAULT_WAIT_MS,
        )
    except Exception as e:
        elapsed = time.time() - t0
        _last_fetch_at = time.time()
        print(f"  browser_fetch_err {type(e).__name__}: {str(e)[:160]}")
        return None, None, elapsed

    elapsed = time.time() - t0
    _last_fetch_at = time.time()
    status = (getattr(page, "status", None)
              or getattr(page, "status_code", None)
              or 200)
    return page, status, elapsed
