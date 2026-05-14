"""JS source handler.

Sources classified as type='js' load their data via JavaScript after
page render. This handler routes to engines.html_scraper.run_browser()
which uses StealthyFetcher (Playwright-backed) to render the page,
then runs the same cascade extraction the static engine uses.
"""

import csv
import importlib
import os
import time

from engines import html_scraper


def _count_rows(csv_path):
    if not csv_path or not os.path.exists(csv_path):
        return 0
    with open(csv_path, "r", encoding="utf-8", newline="") as f:
        n = sum(1 for _ in csv.reader(f))
    return max(0, n - 1)


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

    # Default path: browser-render via the generic engine.
    try:
        return html_scraper.run_browser(source)
    except Exception as e:
        return {"status": "failure", "record_count": 0,
                "runtime_seconds": round(time.time() - start, 2),
                "error": f"engine {type(e).__name__}: {e}",
                "csv_path": None,
                "fetch_tier": "browser"}
