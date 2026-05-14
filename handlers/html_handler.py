"""HTML source handler.

Three-tier dispatch:
  1. Custom scraper in scrapers/<file>.py if sources.json declares one.
  2. Tier-1 generic engine (static fetch + cascade extraction) via
     engines.html_scraper.run().
  3. Tier-2 generic engine (browser-rendered fetch via StealthyFetcher)
     via engines.html_scraper.run_browser() - triggered automatically
     when Tier-1 returns thin/raw_text output AND the page body shows
     JS-render indicators (loading text, React/Angular markers, empty
     <tbody>, <noscript>, etc.).
"""

import csv
import importlib
import os
import time

from engines import html_scraper, browser_fetcher, pdf_discovery

MIN_STRUCTURED_ROWS = 3   # below this we consider Tier-1 'thin'


def _count_rows(csv_path):
    if not csv_path or not os.path.exists(csv_path):
        return 0
    with open(csv_path, "r", encoding="utf-8", newline="") as f:
        n = sum(1 for _ in csv.reader(f))
    return max(0, n - 1)


def _is_tier1_thin(result):
    """True if the Tier-1 result is 'good enough' to skip Tier-2.
    A Tier-1 result is *thin* if it succeeded but with raw_text
    fallback or fewer than MIN_STRUCTURED_ROWS, or if it failed
    outright."""
    if result["status"] != "success":
        return True
    if result.get("extraction_strategy") == "raw_text":
        return True
    if result["record_count"] < MIN_STRUCTURED_ROWS:
        return True
    return False


def _is_browser_better(tier1, tier2):
    """Decide which result to keep. Prefer browser if it has more
    structured rows OR succeeded where static failed/got raw_text.
    Skipped (bot-blocked) Tier-2 means static stays."""
    if tier2["status"] != "success":
        return False
    if tier1["status"] != "success":
        return True
    s1, s2 = tier1.get("extraction_strategy"), tier2.get("extraction_strategy")
    # Prefer structured over raw_text
    if s1 == "raw_text" and s2 in ("table", "blocks"):
        return True
    if s1 in ("table", "blocks") and s2 == "raw_text":
        return False
    # Both structured (or both raw_text): more rows wins
    return tier2["record_count"] > tier1["record_count"]


def handle(source):
    start = time.time()
    scraper_file = source.get("scraper")

    if scraper_file:
        module_name = scraper_file[:-3] if scraper_file.endswith(".py") else scraper_file
        try:
            module = importlib.import_module(f"scrapers.{module_name}")
            module.run()
            csv_path = getattr(module, "OUTPUT_FILE", None)
            return {"status": "success",
                    "record_count": _count_rows(csv_path),
                    "runtime_seconds": round(time.time() - start, 2),
                    "error": None,
                    "csv_path": csv_path,
                    "fetch_tier": "custom"}
        except Exception as e:
            return {"status": "failure", "record_count": 0,
                    "runtime_seconds": round(time.time() - start, 2),
                    "error": f"{type(e).__name__}: {e}",
                    "csv_path": None,
                    "fetch_tier": "custom"}

    # Tier 1: static fetch via the generic engine.
    try:
        tier1 = html_scraper.run(source)
    except Exception as e:
        tier1 = {"status": "failure", "record_count": 0,
                 "runtime_seconds": round(time.time() - start, 2),
                 "error": f"engine {type(e).__name__}: {e}",
                 "csv_path": None, "extraction_strategy": None,
                 "fetch_tier": "static"}

    if not _is_tier1_thin(tier1):
        return tier1

    # Tier-2 decision: only attempt if static body looked JS-shaped.
    # We need the static fetched body to test that, so re-do a quick
    # static fetch JUST for the indicator check (cheap; ~1 fetch).
    js_indicated = False
    url = source.get("url")
    if url:
        try:
            from scrapling import Fetcher
            probe = Fetcher.get(url, timeout=10, retries=1, retry_delay=0,
                                verify=False)
            js_indicated = browser_fetcher.is_js_indicated(probe)
        except Exception:
            js_indicated = False

    if not js_indicated:
        # Tier-1 was thin but page didn't look JS-rendered - leave it.
        return tier1

    print(f"  [{source.get('id')}] static thin + JS indicators present; "
          f"retrying with browser render")
    try:
        tier2 = html_scraper.run_browser(source)
    except Exception as e:
        tier2 = {"status": "failure", "record_count": 0,
                 "runtime_seconds": round(time.time() - start, 2),
                 "error": f"browser_engine {type(e).__name__}: {e}",
                 "csv_path": None, "extraction_strategy": None,
                 "fetch_tier": "browser"}

    final = tier2 if _is_browser_better(tier1, tier2) else tier1

    # Tier-3 fallback: pdf_discovery. If both Tier-1 and Tier-2 are still
    # thin (<3 structured rows or raw_text), check whether the landing
    # page links to AML-relevant downloadable files we can parse.
    if _is_tier1_thin(final):
        try:
            tier3 = pdf_discovery.run(source)
        except Exception as e:
            tier3 = {"status": "failure", "record_count": 0,
                     "error": f"pdf_discovery {type(e).__name__}: {e}",
                     "csv_path": None,
                     "fetch_tier": "discovery"}
        if tier3.get("status") == "success" and \
           tier3.get("record_count", 0) > final.get("record_count", 0):
            print(f"  [{source.get('id')}] pdf_discovery upgrade: "
                  f"{final.get('record_count', 0)} -> "
                  f"{tier3['record_count']} via attachments")
            return tier3
    return final
