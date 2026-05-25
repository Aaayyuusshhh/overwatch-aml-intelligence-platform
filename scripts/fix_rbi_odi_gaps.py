#!/usr/bin/env python3
"""Fix all month-coverage gaps in rbi_odi_investments.

Two distinct bugs caused the missing months:

  1. The earlier fix_rbi_odi_periods.py used the *release date* embedded in
     filenames (DDMMYY) as the data period, but each RBI ODI press release
     reports the PREVIOUS month's data. This caused 16 files to be shifted
     forward by one month: the cascade effect means each "true" month is
     actually present in a file whose DB label is wrong.

  2. Seven cached Excel files have a leading empty column (Sr in col 1,
     not col 0). The original parser's hardcoded indices found 0 data
     rows; the updated parser uses dynamic column mapping.

Strategy:
  - Re-parse every cached file with the current parser
  - For each file, compute the *actual* period_from/period_to from the file
  - UPDATE rows whose DB period_from differs from the file's actual period
  - INSERT rows for the 7 files that had no DB rows at all (only if the
    month isn't already covered by another file - avoid duplicates)
"""
import os
import sys
from datetime import datetime, timezone

import psycopg2
import psycopg2.extras

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from scrapers.rbi_odi_investments import parse_excel  # noqa: E402

PROJECT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CACHE_DIR = os.path.join(PROJECT, "data", "rbi_odi")

LOCAL = dict(host="localhost", user="aayush", password="aayush123", dbname="risk_pipeline")
RDS = dict(
    host="overwatch-aml.cnsgg0i0cxa7.ap-south-1.rds.amazonaws.com",
    user="aayush", password="Aaayyuusshhh", dbname="risk_pipeline",
    connect_timeout=30,
)

INSERT_COLS = [
    "prid", "press_release_url", "excel_url", "excel_filename",
    "period", "period_from", "period_to",
    "sr_no", "indian_party", "jv_wos_name", "jv_or_wos",
    "country", "activity",
    "equity_usd_mn", "loan_usd_mn", "guarantee_usd_mn", "total_usd_mn",
    "scraped_at",
]

# Files we know are multi-sheet cumulative; don't touch (their period rotates
# per row and was already fixed by fix_rbi_odi_cumulative_file.py).
SKIP_FILES = {"OFDIM220611.xls"}


def categorise(conn_kwargs):
    """Return (to_update, to_insert) lists.
    to_update: [(filename, db_period_from, actual_period_from, actual_period_to), ...]
    to_insert: [(filename, parsed_rows, prid, press_url, excel_url), ...]
    """
    conn = psycopg2.connect(**conn_kwargs)
    cur = conn.cursor()
    cur.execute("""
        SELECT DISTINCT excel_filename, period_from, period_to
        FROM rbi_odi_investments
        WHERE excel_filename IS NOT NULL;
    """)
    db_periods = {}
    for fname, pf, pt in cur.fetchall():
        db_periods.setdefault(fname, set()).add((pf, pt))

    conn.close()

    to_update = []
    to_insert = []

    for fname in sorted(os.listdir(CACHE_DIR)):
        if fname.startswith("_") or fname.endswith(".json") or fname in SKIP_FILES:
            continue
        path = os.path.join(CACHE_DIR, fname)
        if not os.path.isfile(path) or os.path.getsize(path) < 1000:
            continue
        _, rows = parse_excel(path)
        if not rows:
            continue
        periods = set(r.get("period_from") for r in rows if r.get("period_from"))
        if not periods:
            continue
        if len(periods) > 1:
            # Multi-sheet file (skip - SKIP_FILES already covers the known one)
            continue
        actual_pf = next(iter(periods))
        actual_pt = next(iter(set(r.get("period_to") for r in rows if r.get("period_to"))), None)

        if fname not in db_periods:
            # Not in DB at all -> insert (no metadata available)
            to_insert.append((fname, rows, "", "", ""))
        else:
            db_pf_set = set(p[0] for p in db_periods[fname])
            if len(db_pf_set) == 1 and next(iter(db_pf_set)) != actual_pf:
                db_pf = next(iter(db_pf_set))
                to_update.append((fname, db_pf, actual_pf, actual_pt))

    return to_update, to_insert


def build_insert_rows(filename, rows, prid, press_url, excel_url, scraped_at):
    out = []
    for r in rows:
        out.append((
            prid, press_url, excel_url, filename,
            r.get("period", ""), r.get("period_from"), r.get("period_to"),
            r.get("sr_no"), r["indian_party"], r.get("jv_wos_name", ""),
            r.get("jv_or_wos", ""), r.get("country", ""), r.get("activity", ""),
            r.get("equity_usd_mn", 0.0), r.get("loan_usd_mn", 0.0),
            r.get("guarantee_usd_mn", 0.0), r.get("total_usd_mn", 0.0),
            scraped_at,
        ))
    return out


def apply(label, conn_kwargs, to_update, to_insert):
    conn = psycopg2.connect(**conn_kwargs)
    cur = conn.cursor()

    n_updated_rows = 0
    for fname, db_pf, actual_pf, actual_pt in to_update:
        cur.execute("""
            UPDATE rbi_odi_investments
            SET period_from = %s, period_to = %s
            WHERE excel_filename = %s AND period_from = %s;
        """, (actual_pf, actual_pt, fname, db_pf))
        n_updated_rows += cur.rowcount

    scraped_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    n_inserted_rows = 0
    inserted_files = []

    # Filter to_insert: only insert files whose period is NOT already in DB
    # via some other file (avoid duplicate months from different RBI re-issues).
    cur.execute("""
        SELECT DISTINCT period_from FROM rbi_odi_investments
        WHERE period_from IS NOT NULL AND period_from != '';
    """)
    existing_months = set(r[0] for r in cur.fetchall())

    for fname, rows, prid, press_url, excel_url in to_insert:
        actual_pf = next(iter(set(r.get("period_from") for r in rows if r.get("period_from"))), None)
        if actual_pf is None:
            continue
        if actual_pf in existing_months:
            print(f"  [{label}] skip {fname}: {actual_pf} already in DB via another file")
            continue
        insert_rows = build_insert_rows(fname, rows, prid, press_url, excel_url, scraped_at)
        psycopg2.extras.execute_values(
            cur,
            f"INSERT INTO rbi_odi_investments ({','.join(INSERT_COLS)}) VALUES %s",
            insert_rows, page_size=1000,
        )
        n_inserted_rows += len(insert_rows)
        inserted_files.append((fname, len(insert_rows), actual_pf))
        existing_months.add(actual_pf)

    conn.commit()
    print(f"\n[{label}] UPDATE: {n_updated_rows:,} rows shifted across {len(to_update)} files")
    for fname, db_pf, actual_pf, _ in to_update:
        print(f"  {fname:35s} {db_pf} -> {actual_pf}")
    print(f"\n[{label}] INSERT: {n_inserted_rows:,} rows from {len(inserted_files)} new files")
    for fname, n, pf in inserted_files:
        print(f"  {fname:35s} +{n} rows ({pf})")

    cur.execute("SELECT COUNT(*) FROM rbi_odi_investments;")
    total = cur.fetchone()[0]
    print(f"\n[{label}] table total: {total:,}")
    conn.close()


if __name__ == "__main__":
    print("Categorising changes by scanning each cached Excel...")
    to_update, to_insert = categorise(LOCAL)
    print(f"-> {len(to_update)} files to UPDATE (shift period), {len(to_insert)} files to INSERT")
    apply("local", LOCAL, to_update, to_insert)
    apply("RDS  ", RDS, to_update, to_insert)
