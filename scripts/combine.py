"""
Combine all per-source CSVs in data/ into data/master_watchlist.csv.

Per ARCHITECTURE.md §3.6, §4.1.5 / PRD §6.8. Validates every source
CSV shares the canonical 17-column schema before concatenating, so a
malformed scraper output cannot corrupt the master file.
"""

import os
import sys
from datetime import datetime

import pandas as pd

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.path.join(PROJECT_ROOT, "data")
MASTER_PATH = os.path.join(DATA_DIR, "master_watchlist.csv")

CANONICAL_COLUMNS = [
    "source_agency", "source_list", "case_unit",
    "name", "father_name", "date_of_birth", "gender",
    "address", "reward_amount", "details",
    "has_document", "document_url", "detail_page_url",
    "interpol_notice_id", "link_kind", "scraped_at",
    "enrichment_status",
]


def list_source_csvs():
    """Every data/*.csv except master_watchlist.csv itself."""
    out = []
    for name in sorted(os.listdir(DATA_DIR)):
        if not name.endswith(".csv"):
            continue
        if name == "master_watchlist.csv":
            continue
        out.append(os.path.join(DATA_DIR, name))
    return out


def combine():
    print(f"[combine] Scanning {DATA_DIR}")
    sources = list_source_csvs()
    print(f"[combine] Found {len(sources)} per-source CSVs")

    frames = []
    mismatches = []
    for path in sources:
        try:
            df = pd.read_csv(path, dtype=str, keep_default_na=False)
        except Exception as e:
            mismatches.append((path, f"read error: {type(e).__name__}: {e}"))
            continue
        cols = list(df.columns)
        if cols != CANONICAL_COLUMNS:
            mismatches.append((path, f"schema differs (got {len(cols)} cols)"))
            continue
        frames.append(df)
        print(f"[combine]   {os.path.basename(path)}: {len(df)} rows")

    if mismatches:
        print(f"[combine] SCHEMA MISMATCHES ({len(mismatches)}):")
        for path, reason in mismatches:
            print(f"           {os.path.basename(path)}: {reason}")

    if not frames:
        print("[combine] No valid frames — not writing master.")
        return 0, len(mismatches)

    master = pd.concat(frames, ignore_index=True)
    master.to_csv(MASTER_PATH, index=False)
    total = len(master)
    print(f"[combine] Wrote {total} rows to {MASTER_PATH}")
    return total, len(mismatches)


if __name__ == "__main__":
    rows, errs = combine()
    sys.exit(0 if errs == 0 else 1)
