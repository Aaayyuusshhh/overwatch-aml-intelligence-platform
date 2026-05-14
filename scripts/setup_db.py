"""
setup_db.py - Create the watchlist_records table in PostgreSQL.

Idempotent: uses CREATE TABLE IF NOT EXISTS so it is safe to re-run.

The table holds the 17-column shared schema (per ARCHITECTURE.md S6.1)
plus three operational columns:
  - id          serial primary key
  - source_id   tracks which sources.json entry produced the row
  - loaded_at   timestamp of last DB load

Connection params come from env vars when present; otherwise local
defaults (the values the engineer set in TASK A2 of Phase 2):
  PGHOST     default 'localhost'
  PGPORT     default 5432
  PGDATABASE default 'risk_pipeline'
  PGUSER     default 'aayush'
  PGPASSWORD default 'aayush123'

Usage:
    python scripts/setup_db.py
"""

import os
import sys

import psycopg2

DB_CONFIG = {
    "host": os.environ.get("PGHOST", "localhost"),
    "port": int(os.environ.get("PGPORT", 5432)),
    "dbname": os.environ.get("PGDATABASE", "risk_pipeline"),
    "user": os.environ.get("PGUSER", "aayush"),
    "password": os.environ.get("PGPASSWORD", "aayush123"),
}

DDL = """
CREATE TABLE IF NOT EXISTS watchlist_records (
    id                 SERIAL PRIMARY KEY,
    source_id          TEXT,
    source_agency      TEXT,
    source_list        TEXT,
    case_unit          TEXT,
    name               TEXT,
    father_name        TEXT,
    date_of_birth      TEXT,
    gender             TEXT,
    address            TEXT,
    reward_amount      TEXT,
    details            TEXT,
    has_document       TEXT,
    document_url       TEXT,
    detail_page_url    TEXT,
    interpol_notice_id TEXT,
    link_kind          TEXT,
    scraped_at         TEXT,
    enrichment_status  TEXT,
    loaded_at          TIMESTAMP NOT NULL DEFAULT NOW()
);
"""

INDEXES = [
    "CREATE INDEX IF NOT EXISTS idx_watchlist_source_agency ON watchlist_records (source_agency);",
    "CREATE INDEX IF NOT EXISTS idx_watchlist_source_list   ON watchlist_records (source_list);",
    "CREATE INDEX IF NOT EXISTS idx_watchlist_source_id     ON watchlist_records (source_id);",
    "CREATE INDEX IF NOT EXISTS idx_watchlist_name          ON watchlist_records (name);",
]


def connect():
    return psycopg2.connect(**DB_CONFIG)


def main():
    print(f"Connecting to {DB_CONFIG['user']}@{DB_CONFIG['host']}:{DB_CONFIG['port']}/{DB_CONFIG['dbname']} ...")
    try:
        conn = connect()
    except psycopg2.OperationalError as e:
        print(f"FATAL: cannot connect: {e}", file=sys.stderr)
        sys.exit(2)

    with conn:
        with conn.cursor() as cur:
            cur.execute(DDL)
            for stmt in INDEXES:
                cur.execute(stmt)
            cur.execute("""
                SELECT column_name, data_type
                  FROM information_schema.columns
                 WHERE table_name = 'watchlist_records'
                 ORDER BY ordinal_position;
            """)
            cols = cur.fetchall()
    conn.close()

    print(f"OK: watchlist_records has {len(cols)} columns:")
    for name, dtype in cols:
        print(f"  {name:22} {dtype}")


if __name__ == "__main__":
    main()
