"""
load_to_db.py - Load data/master_watchlist.csv into PostgreSQL.

Refresh-safe: for every (source_agency, source_list) pair present in
the CSV, existing rows are deleted before inserting fresh ones, all
inside one transaction. Sources not in the current CSV are left
untouched.

Resolves source_id by looking up the (source_agency, source_list) pair
in sources.json; missing matches fall back to ''.

Uses psycopg2.extras.execute_values for batch INSERT (fast enough at
the current 1-10k row scale; if volume grows, swap to COPY).

Usage:
    python scripts/load_to_db.py
    python scripts/load_to_db.py --csv path/to/other.csv
"""

import argparse
import csv
import json
import os
import sys
from collections import defaultdict

import psycopg2
import psycopg2.extras

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_CSV = os.path.join(PROJECT_ROOT, "data", "master_watchlist.csv")
SOURCES_JSON = os.path.join(PROJECT_ROOT, "sources.json")

DB_CONFIG = {
    "host": os.environ.get("PGHOST", "localhost"),
    "port": int(os.environ.get("PGPORT", 5432)),
    "dbname": os.environ.get("PGDATABASE", "risk_pipeline"),
    "user": os.environ.get("PGUSER", "aayush"),
    "password": os.environ.get("PGPASSWORD", "aayush123"),
}

# Order MUST match the INSERT column order below.
SCHEMA_COLS = [
    "source_id", "source_agency", "source_list", "case_unit",
    "name", "father_name", "date_of_birth", "gender", "address",
    "reward_amount", "details", "has_document", "document_url",
    "detail_page_url", "interpol_notice_id", "link_kind",
    "scraped_at", "enrichment_status",
]

INSERT_SQL = f"""
INSERT INTO watchlist_records ({", ".join(SCHEMA_COLS)})
VALUES %s
"""


def load_source_id_map():
    """Map (source_agency, source_list) -> source_id from sources.json."""
    if not os.path.exists(SOURCES_JSON):
        return {}
    with open(SOURCES_JSON, "r", encoding="utf-8") as f:
        data = json.load(f)
    out = {}
    for s in data.get("sources", []):
        key = (s.get("agency", "").strip(), s.get("list_name", "").strip())
        out[key] = s.get("id", "")
    return out


def read_csv_rows(csv_path):
    """Yield (source_agency, source_list, row_tuple) for each CSV row."""
    with open(csv_path, "r", encoding="utf-8", newline="") as f:
        reader = csv.DictReader(f)
        return list(reader)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--csv", default=DEFAULT_CSV,
                    help=f"input CSV (default: {DEFAULT_CSV})")
    ap.add_argument("--dry-run", action="store_true",
                    help="parse CSV but do not touch the database")
    args = ap.parse_args()

    if not os.path.exists(args.csv):
        print(f"FATAL: input CSV not found: {args.csv}", file=sys.stderr)
        sys.exit(2)

    rows = read_csv_rows(args.csv)
    print(f"Read {len(rows)} rows from {args.csv}")
    if not rows:
        print("Nothing to load.")
        return

    sid_map = load_source_id_map()
    print(f"sources.json provides {len(sid_map)} (agency, list_name) -> id mappings")

    # Prepare INSERT tuples and the set of (agency, list) pairs to refresh.
    refresh_pairs = set()
    insert_rows = []
    missing_sid = 0
    for r in rows:
        agency = (r.get("source_agency") or "").strip()
        slist = (r.get("source_list") or "").strip()
        sid = sid_map.get((agency, slist), "")
        if not sid:
            missing_sid += 1
        refresh_pairs.add((agency, slist))
        insert_rows.append((
            sid,
            agency, slist,
            r.get("case_unit", ""),
            r.get("name", ""), r.get("father_name", ""),
            r.get("date_of_birth", ""), r.get("gender", ""),
            r.get("address", ""), r.get("reward_amount", ""),
            r.get("details", ""), r.get("has_document", ""),
            r.get("document_url", ""), r.get("detail_page_url", ""),
            r.get("interpol_notice_id", ""), r.get("link_kind", ""),
            r.get("scraped_at", ""), r.get("enrichment_status", ""),
        ))

    if missing_sid:
        print(f"NOTE: {missing_sid} rows had no matching source_id in sources.json")

    if args.dry_run:
        print(f"DRY RUN: would refresh {len(refresh_pairs)} sources, "
              f"insert {len(insert_rows)} rows")
        return

    print(f"Connecting to {DB_CONFIG['user']}@{DB_CONFIG['host']}/{DB_CONFIG['dbname']} ...")
    conn = psycopg2.connect(**DB_CONFIG)
    deleted_total = 0
    try:
        with conn:
            with conn.cursor() as cur:
                # Delete-then-insert per (agency, list) pair, all in one txn.
                if refresh_pairs:
                    cur.executemany(
                        "DELETE FROM watchlist_records "
                        "WHERE source_agency = %s AND source_list = %s",
                        list(refresh_pairs),
                    )
                    deleted_total = cur.rowcount  # last DELETE only; informational
                psycopg2.extras.execute_values(
                    cur, INSERT_SQL, insert_rows, page_size=500,
                )
                cur.execute("SELECT COUNT(*) FROM watchlist_records;")
                total = cur.fetchone()[0]
    finally:
        conn.close()

    print(f"Refreshed sources    : {len(refresh_pairs)}")
    print(f"Rows inserted        : {len(insert_rows)}")
    print(f"Total rows in table  : {total}")


if __name__ == "__main__":
    main()
