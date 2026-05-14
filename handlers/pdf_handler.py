"""PDF source handler.

Routes to the source's custom scraper (scrapers/<file>.py) when
sources.json declares one, otherwise falls back to the generic
engine at engines/pdf_scraper.py.

Custom-scraper path remains 'verify-only': if the scraper exposes
PDF_PATH and the local file is missing, we skip with a clear reason
(we do NOT auto-download for custom scrapers - they manage their
own input files).
"""

import csv
import importlib
import os
import time

from engines import pdf_scraper


def _count_rows(csv_path):
    """Count data rows using csv.reader (handles embedded newlines)."""
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
        except Exception as e:
            return {"status": "failure", "record_count": 0,
                    "runtime_seconds": round(time.time() - start, 2),
                    "error": f"import {type(e).__name__}: {e}",
                    "csv_path": None}

        # Verify-only: if scraper exposes PDF_PATH and the file is missing,
        # skip with reason. We do NOT auto-download for custom scrapers.
        pdf_path = getattr(module, "PDF_PATH", None)
        if pdf_path and not os.path.exists(pdf_path):
            return {"status": "skipped", "record_count": 0,
                    "runtime_seconds": round(time.time() - start, 2),
                    "error": f"local PDF missing at {pdf_path}",
                    "csv_path": None}

        try:
            module.run()
            csv_path = getattr(module, "OUTPUT_FILE", None)
            return {"status": "success",
                    "record_count": _count_rows(csv_path),
                    "runtime_seconds": round(time.time() - start, 2),
                    "error": None,
                    "csv_path": csv_path}
        except Exception as e:
            return {"status": "failure", "record_count": 0,
                    "runtime_seconds": round(time.time() - start, 2),
                    "error": f"{type(e).__name__}: {e}",
                    "csv_path": None}

    # No custom scraper -> generic PDF engine. Returned dict contains the
    # framework keys plus PDF-specific fields (pages_processed, etc.);
    # main.py ignores extra keys so we leave them in for visibility.
    try:
        return pdf_scraper.run(source)
    except Exception as e:
        return {"status": "failure", "record_count": 0,
                "runtime_seconds": round(time.time() - start, 2),
                "error": f"engine {type(e).__name__}: {e}",
                "csv_path": None}
