"""
network_capture.py - Use Playwright to load JS-walled pages with a
response-listener attached, then report every interesting XHR/fetch
the browser makes. Goal: discover the backend API a JS portal calls,
so we can replay that call directly from a static-fetch scraper.

Usage:
    python scripts/network_capture.py
"""

import re
import sys
from playwright.sync_api import sync_playwright

# Asset extensions / patterns we never care about.
ASSET_RE = re.compile(
    r"\.(?:js|css|png|jpe?g|gif|svg|webp|ico|woff2?|ttf|otf|map|mp4)(?:\?|$)",
    re.I,
)
ASSET_HOSTS = (
    "google-analytics.com", "googletagmanager.com", "facebook.com",
    "doubleclick.net", "fonts.googleapis.com", "fonts.gstatic.com",
    "static.cloudflareinsights.com", "cdn.jsdelivr.net",
    "polyfill.io", "google.com/recaptcha", "www.youtube.com",
    "twitter.com", "linkedin.com", "newrelic", "matomo",
)


def is_asset(url):
    if ASSET_RE.search(url):
        return True
    return any(h in url for h in ASSET_HOSTS)


def is_interesting(url, status, content_type, size):
    """Filter for likely-data responses: not assets, > 1KB, JSON / HTML
    / XML / text content type."""
    if status >= 400:
        return False
    if is_asset(url):
        return False
    if size < 1024:
        return False
    ct = (content_type or "").lower()
    return ("json" in ct or "xml" in ct or "html" in ct or
            "text/plain" in ct or "csv" in ct or
            "x-www-form-urlencoded" in ct or "octet-stream" in ct)


def capture(label, url, interact=None, wait_ms=8000):
    """Open url with Playwright, run optional interact(page) callback
    to trigger XHRs, then dump the interesting responses."""
    print("=" * 84)
    print(f"{label}")
    print(f"  url: {url}")
    captured = []

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        try:
            context = browser.new_context(
                user_agent="Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                           "(KHTML, like Gecko) Chrome/124.0 Safari/537.36",
                ignore_https_errors=True,
            )
            page = context.new_page()

            def on_response(resp):
                try:
                    sz = 0
                    try:
                        sz = len(resp.body())
                    except Exception:
                        sz = 0
                    captured.append({
                        "url": resp.url,
                        "status": resp.status,
                        "type": resp.headers.get("content-type", ""),
                        "size": sz,
                        "method": resp.request.method,
                    })
                except Exception:
                    pass

            page.on("response", on_response)

            try:
                page.goto(url, wait_until="networkidle", timeout=45_000)
            except Exception as e:
                print(f"  goto error: {type(e).__name__}: {str(e)[:160]}")

            page.wait_for_timeout(wait_ms)

            if interact:
                try:
                    interact(page)
                    page.wait_for_timeout(5_000)
                except Exception as e:
                    print(f"  interact error: {type(e).__name__}: {str(e)[:160]}")
        finally:
            browser.close()

    interesting = [r for r in captured
                   if is_interesting(r["url"], r["status"], r["type"], r["size"])]
    print(f"  total responses captured: {len(captured)}")
    print(f"  interesting (>1KB, not asset, json/html/xml): {len(interesting)}")
    interesting.sort(key=lambda x: -x["size"])
    for r in interesting[:25]:
        print(f"    {r['method']:5s} {r['status']:3d} {r['size']:>8d}  {r['type'][:30]:30s}  {r['url'][:120]}")
    return interesting


# ---------------------------------------------------------------------------
# Per-source interaction callbacks
# ---------------------------------------------------------------------------
def click_any_button(page):
    """Try to click a few obvious 'Show'/'Submit'/'Search' buttons that
    might trigger a data XHR."""
    for sel in ("button:has-text('Submit')", "input[type=submit]",
                "button:has-text('Search')", "button:has-text('Show')",
                "button:has-text('View')"):
        try:
            el = page.query_selector(sel)
            if el and el.is_visible():
                el.click()
                page.wait_for_timeout(3_000)
                print(f"  clicked: {sel}")
                return
        except Exception:
            continue


def fcra_interact(page):
    """FCRA: select a state from ddlstate, click Submit; capture the XHR
    that returns the deemed-association list."""
    try:
        page.select_option("select[name=ddlstate]", value="01")  # Andhra Pradesh
        page.wait_for_timeout(2_000)
        # The page also has a state-only dropdown trigger via __doPostBack
        # so the state change itself may already trigger a postback.
    except Exception as e:
        print(f"  fcra select state err: {e}")
    try:
        # Look for radio button rdlist=2 (sometimes "show all")
        for v in ("0", "1", "2"):
            try:
                page.check(f"input[name=rdlist][value='{v}']")
            except Exception:
                pass
        # Submit button
        for sel in ("input[name=btsubmitt]", "input[value=Submit]",
                    "input[type=submit]"):
            el = page.query_selector(sel)
            if el and el.is_visible():
                el.click()
                page.wait_for_timeout(4_000)
                print(f"  fcra clicked submit via {sel}")
                break
    except Exception as e:
        print(f"  fcra submit err: {e}")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
TARGETS = [
    ("BSE_defaulters_159",
     "https://www.bseindia.com/members/defaulters_list.aspx",
     click_any_button),
    ("CVC_penalties_16",
     "https://cvc.gov.in/penalties-for-prosecution",
     None),
    ("CBEC_cx_fraud_6",
     "https://www.cbic.gov.in/htdocs-cbec/excise/cx-fraud",
     None),
    ("DMRC_141",
     "https://www.delhimetrorail.com/",
     click_any_button),
    ("FCRA_71",
     "https://fcraonline.nic.in/fc_deemed_asso_list.aspx",
     fcra_interact),
]


def main():
    findings = {}
    for label, url, interact in TARGETS:
        try:
            interesting = capture(label, url, interact=interact)
        except Exception as e:
            print(f"  capture exception for {label}: {type(e).__name__}: {e}")
            interesting = []
        findings[label] = interesting
        print()
    return findings


if __name__ == "__main__":
    main()
