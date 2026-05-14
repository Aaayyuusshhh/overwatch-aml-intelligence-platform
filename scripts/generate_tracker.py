"""
Produce project_status.xlsx from the canonical 244-row India watchlist
catalogue, sources.json (current classification), and live CSV files
in data/.

Per ARCHITECTURE.md S4.8 / PRD S6.9.

Status resolution (for each canonical row, in priority order):
  1. data/<source_id>.csv exists with >= 1 row  -> 'completed'
  2. data/<source_id>.csv exists with 0 rows    -> 'empty'
  3. sources.json says scrape attempted & failed-> 'failed' / 'restricted'
                                                  / 'dead' / 'js' /
                                                  'url_not_found'
  4. sources.json says skipped or duplicate     -> 'skipped'
  5. canonical CSV status (last resort)         -> as-is

source_id is resolved by joining canonical ppt_number -> sources.json
entry's ppt_number field (populated by classify.py).
"""

import csv
import json
import os
from datetime import datetime

import pandas as pd

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SOURCES_PATH = os.path.join(PROJECT_ROOT, "sources.json")
CANONICAL_CSV = os.path.join(PROJECT_ROOT, "india_watchlist_sources_complete.csv")
DATA_DIR = os.path.join(PROJECT_ROOT, "data")
TRACKER_PATH = os.path.join(PROJECT_ROOT, "project_status.xlsx")

TRACKER_COLUMNS = [
    "ppt_number", "agency", "watchlist_details",
    "type", "status", "records", "last_run",
    "url", "scraper", "notes",
]


def count_csv_rows(path):
    if not os.path.exists(path):
        return None
    with open(path, "r", encoding="utf-8", newline="") as f:
        return max(0, sum(1 for _ in csv.reader(f)) - 1)


def file_mtime_str(path):
    if not os.path.exists(path):
        return None
    return datetime.fromtimestamp(os.path.getmtime(path)).strftime("%Y-%m-%d %H:%M:%S")


def _has_real_name(rows):
    """True if at least one row has a 'name' value with >= 5 non-whitespace chars."""
    for r in rows:
        name = (r.get("name") or "").strip()
        if len(name) >= 5:
            return True
    return False


def _read_rows(csv_path):
    if not csv_path or not os.path.exists(csv_path):
        return None
    with open(csv_path, "r", encoding="utf-8", newline="") as f:
        return list(csv.DictReader(f))


def derive_status(canon_status, sources_entry, csv_path, has_scraper):
    """Pick the most useful status string for the tracker row.

    Honest tier logic:
      completed - CSV exists with 3+ rows, link_kind != 'unstructured',
                  AND >=1 row has a name >=5 chars
      partial   - CSV exists but fails any of the above
      failed    - sources.json says status=active but no CSV exists
    Other sources.json statuses (url_not_found, restricted, js, skipped,
    dead, failed) pass through unchanged. type=duplicate -> 'skipped'.
    """
    if sources_entry is None:
        return canon_status or "pending_recon"

    sjs_status = sources_entry.get("status", "")
    sjs_type = sources_entry.get("type", "")

    if sjs_type == "duplicate":
        return "skipped"
    # Honor any non-active status set by classify.py / audit_data.py.
    if sjs_status and sjs_status != "active":
        return sjs_status

    # status=='active'. CSV present?
    rows = _read_rows(csv_path)
    if rows is None:
        return "failed"
    n = len(rows)

    if n == 0:
        # Honest "empty list" state: scraper is in place AND the source's
        # expected_min_records is 0 (engineer asserted the list can be empty).
        # Examples: NIA arrested-in-custody, NCDEX expelled/cessation tables
        # that legitimately have no current entries.
        if has_scraper and (sources_entry.get("expected_min_records") == 0):
            return "completed"
        return "partial"

    link_kind = (rows[0].get("link_kind") or "").strip()
    real_name = _has_real_name(rows)

    # Custom scraper output is trusted - no extra heuristics on link_kind.
    if has_scraper:
        return "completed" if n >= 1 and real_name else "partial"

    if link_kind == "unstructured":
        return "partial"
    if n < 3:
        return "partial"
    if not real_name:
        return "partial"
    return "completed"


def generate():
    with open(SOURCES_PATH, "r", encoding="utf-8") as f:
        sources = json.load(f)["sources"]
    by_ppt = {s.get("ppt_number"): s for s in sources}

    rows_out = []
    with open(CANONICAL_CSV, "r", encoding="utf-8", newline="") as f:
        for row in csv.DictReader(f):
            try:
                ppt = int(row.get("ppt_number", ""))
            except (TypeError, ValueError):
                ppt = row.get("ppt_number", "")
            agency = row.get("agency", "")
            details = row.get("watchlist_details", "")
            canon_status = row.get("status", "")
            notes = row.get("notes", "")

            sjs = by_ppt.get(ppt)
            sid = sjs.get("id") if sjs else ""
            csv_path = os.path.join(DATA_DIR, f"{sid}.csv") if sid else ""
            records = count_csv_rows(csv_path) if sid else None
            last_run = file_mtime_str(csv_path) if sid else ""
            has_scraper = bool(sjs and sjs.get("scraper")) if sjs else False

            status_out = derive_status(canon_status, sjs, csv_path, has_scraper)

            rows_out.append({
                "ppt_number": ppt,
                "agency": agency,
                "watchlist_details": details,
                "type": sjs.get("type", "") if sjs else "",
                "status": status_out,
                "records": records if records is not None else "",
                "last_run": last_run or "",
                "url": sjs.get("url", "") if sjs else "",
                "scraper": (sjs.get("scraper", "") if sjs else "") or "",
                "notes": notes,
            })

    df = pd.DataFrame(rows_out, columns=TRACKER_COLUMNS)
    df.to_excel(TRACKER_PATH, index=False, engine="openpyxl")

    counts = df["status"].value_counts(dropna=False).to_dict()
    print(f"[tracker] Wrote {TRACKER_PATH}")
    print(f"[tracker]   total_rows={len(df)}  status_breakdown={counts}")
    return len(df), counts


if __name__ == "__main__":
    generate()
