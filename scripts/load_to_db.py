"""load_to_db.py - per-source targeted refresh of watchlist_records.

Refactored 2026-05-22. Replaces the old executemany version that hung on 2M+
rows. Key changes:
  * COPY FROM STDIN (10-100x faster than execute_values for bulk insert)
  * Per-(source_agency, source_list) commits so progress survives a crash
  * Skips pairs where CSV row count equals current DB row count (no-op)
  * Creates an index on (source_agency, source_list) so per-pair DELETE is
    an index seek instead of a 4.8M-row sequential scan
  * NEVER does `DELETE FROM watchlist_records` (would cascade-wipe the KG)

Usage:
    python scripts/load_to_db.py
    python scripts/load_to_db.py --csv path/to/other.csv
    python scripts/load_to_db.py --dry-run
    python scripts/load_to_db.py --limit-sources 5    # smoke test
    python scripts/load_to_db.py --force-all          # ignore no-diff skip
"""
import argparse
import csv
import io
import json
import os
import sys
import time
from collections import defaultdict

import psycopg2

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

COPY_COLS = (
    "source_id, source_agency, source_list, case_unit, name, father_name, "
    "date_of_birth, gender, address, reward_amount, details, has_document, "
    "document_url, detail_page_url, interpol_notice_id, link_kind, "
    "scraped_at, enrichment_status"
)


def load_sid_map():
    with open(SOURCES_JSON, encoding="utf-8") as f:
        data = json.load(f)
    return {
        ((s.get("agency") or "").strip(), (s.get("list_name") or "").strip()):
            s.get("id", "")
        for s in data.get("sources", [])
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--csv", default=DEFAULT_CSV)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--limit-sources", type=int, default=0,
                    help="process only first N pairs (smoke test)")
    ap.add_argument("--force-all", action="store_true",
                    help="refresh every source even if CSV count == DB count")
    args = ap.parse_args()

    t0 = time.time()

    if not os.path.exists(args.csv):
        sys.exit(f"FATAL: input CSV not found: {args.csv}")

    sid_map = load_sid_map()
    print(f"sources.json: {len(sid_map)} (agency, list) -> source_id mappings")

    print(f"Reading {args.csv} ...")
    by_pair = defaultdict(list)
    with open(args.csv, encoding="utf-8", newline="") as f:
        reader = csv.DictReader(f)
        for r in reader:
            ag = (r.get("source_agency") or "").strip()
            ls = (r.get("source_list") or "").strip()
            by_pair[(ag, ls)].append(r)

    total_csv = sum(len(v) for v in by_pair.values())
    print(f"  {total_csv:,} rows across {len(by_pair)} (agency, list) pairs")

    unmatched = [k for k in by_pair if not sid_map.get(k)]
    if unmatched:
        print(f"  NOTE: {len(unmatched)} pairs have no source_id in sources.json "
              f"(will be inserted with source_id='')")

    if args.dry_run:
        print("DRY RUN: no DB changes.")
        return

    conn = psycopg2.connect(**DB_CONFIG)
    conn.autocommit = True  # per-source commits
    cur = conn.cursor()

    print("Ensuring index on (source_agency, source_list) ...")
    t_idx = time.time()
    cur.execute(
        "CREATE INDEX IF NOT EXISTS idx_source_agency_list "
        "ON watchlist_records (source_agency, source_list)"
    )
    print(f"  index ready ({time.time() - t_idx:.1f}s)")

    print("Fetching current DB counts ...")
    t_cnt = time.time()
    cur.execute(
        "SELECT source_agency, source_list, COUNT(*) FROM watchlist_records "
        "GROUP BY source_agency, source_list"
    )
    db_counts = {(ag, ls): n for ag, ls, n in cur.fetchall()}
    print(f"  {len(db_counts)} pairs in DB ({time.time() - t_cnt:.1f}s)")

    pairs = sorted(by_pair.keys())
    if args.limit_sources:
        pairs = pairs[: args.limit_sources]
        print(f"TEST MODE: processing only first {len(pairs)} pairs")

    processed = 0
    skipped = 0
    total_del = 0
    total_ins = 0

    for (ag, ls) in pairs:
        rows = by_pair[(ag, ls)]
        csv_n = len(rows)
        db_n = db_counts.get((ag, ls), 0)
        if not args.force_all and csv_n == db_n and csv_n > 0:
            skipped += 1
            continue
        sid = sid_map.get((ag, ls), "")
        t_pair = time.time()
        cur.execute(
            "DELETE FROM watchlist_records "
            "WHERE source_agency = %s AND source_list = %s",
            (ag, ls),
        )
        d = cur.rowcount
        buf = io.StringIO()
        w = csv.writer(buf, quoting=csv.QUOTE_MINIMAL)
        for r in rows:
            w.writerow([
                sid,
                ag, ls,
                r.get("case_unit", ""),
                r.get("name", ""),
                r.get("father_name", ""),
                r.get("date_of_birth", ""),
                r.get("gender", ""),
                r.get("address", ""),
                r.get("reward_amount", ""),
                r.get("details", ""),
                r.get("has_document", ""),
                r.get("document_url", ""),
                r.get("detail_page_url", ""),
                r.get("interpol_notice_id", ""),
                r.get("link_kind", ""),
                r.get("scraped_at", ""),
                r.get("enrichment_status", ""),
            ])
        buf.seek(0)
        cur.copy_expert(
            f"COPY watchlist_records ({COPY_COLS}) FROM STDIN WITH CSV",
            buf,
        )
        elapsed = time.time() - t_pair
        total_del += d
        total_ins += csv_n
        processed += 1
        tag = sid if sid else f"{ag[:20]}/{ls[:20]}"
        print(f"  [{processed}/{len(pairs)}] {tag}: deleted {d}, "
              f"inserted {csv_n} ({elapsed:.1f}s)")

    cur.execute("SELECT COUNT(*) FROM watchlist_records")
    total = cur.fetchone()[0]
    elapsed = time.time() - t0
    print()
    print("=== load_to_db SUMMARY ===")
    print(f"  Processed sources : {processed}")
    print(f"  Skipped (no diff) : {skipped}")
    print(f"  Rows deleted      : {total_del:,}")
    print(f"  Rows inserted     : {total_ins:,}")
    print(f"  Total in DB       : {total:,}")
    print(f"  Elapsed           : {elapsed:.1f}s ({elapsed / 60:.1f}m)")

    conn.close()


if __name__ == "__main__":
    main()
