#!/usr/bin/env python3
"""Load RBI ODI investments master CSV into local DB + RDS.

This data is NOT a sanctions/watchlist - it goes into its own
rbi_odi_investments table so AML screening does not produce false
positives by treating every Indian company that ever made an overseas
investment as a hit.
"""
import csv
import os
import sys

import psycopg2
import psycopg2.extras

PROJECT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CSV_PATH = os.path.join(PROJECT, "data", "rbi_odi_investments_master.csv")

LOCAL = dict(host="localhost", user="aayush", password="aayush123", dbname="risk_pipeline")
RDS = dict(
    host="overwatch-aml.cnsgg0i0cxa7.ap-south-1.rds.amazonaws.com",
    user="aayush", password="Aaayyuusshhh", dbname="risk_pipeline",
    connect_timeout=30,
)

DDL = """
CREATE TABLE IF NOT EXISTS rbi_odi_investments (
    id SERIAL PRIMARY KEY,
    prid TEXT,
    press_release_url TEXT,
    excel_url TEXT,
    excel_filename TEXT,
    period TEXT,
    period_from TEXT,
    period_to TEXT,
    sr_no INTEGER,
    indian_party TEXT NOT NULL,
    jv_wos_name TEXT,
    jv_or_wos TEXT,
    country TEXT,
    activity TEXT,
    equity_usd_mn NUMERIC(14,4) DEFAULT 0,
    loan_usd_mn NUMERIC(14,4) DEFAULT 0,
    guarantee_usd_mn NUMERIC(14,4) DEFAULT 0,
    total_usd_mn NUMERIC(14,4) DEFAULT 0,
    scraped_at TIMESTAMPTZ DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_odi_indian_party ON rbi_odi_investments (indian_party);
CREATE INDEX IF NOT EXISTS idx_odi_country ON rbi_odi_investments (country);
CREATE INDEX IF NOT EXISTS idx_odi_period_from ON rbi_odi_investments (period_from);
CREATE INDEX IF NOT EXISTS idx_odi_jv_wos ON rbi_odi_investments (jv_wos_name);
"""

INSERT_COLS = [
    "prid", "press_release_url", "excel_url", "excel_filename",
    "period", "period_from", "period_to",
    "sr_no", "indian_party", "jv_wos_name", "jv_or_wos",
    "country", "activity",
    "equity_usd_mn", "loan_usd_mn", "guarantee_usd_mn", "total_usd_mn",
    "scraped_at",
]


def _row_tuple(r):
    def _num(v):
        try:
            return float(v) if v not in (None, "") else 0.0
        except ValueError:
            return 0.0
    def _int(v):
        try:
            return int(float(v)) if v not in (None, "") else None
        except ValueError:
            return None
    return (
        r["prid"], r["press_release_url"], r["excel_url"], r["excel_filename"],
        r["period"], r["period_from"] or None, r["period_to"] or None,
        _int(r["sr_no"]), r["indian_party"], r["jv_wos_name"], r["jv_or_wos"],
        r["country"], r["activity"],
        _num(r["equity_usd_mn"]), _num(r["loan_usd_mn"]),
        _num(r["guarantee_usd_mn"]), _num(r["total_usd_mn"]),
        r["scraped_at"],
    )


def load_into(label, conn_kwargs):
    if not os.path.exists(CSV_PATH):
        print(f"[{label}] CSV not found: {CSV_PATH}")
        return
    rows = []
    with open(CSV_PATH, encoding="utf-8") as f:
        for r in csv.DictReader(f):
            rows.append(_row_tuple(r))
    if not rows:
        print(f"[{label}] no rows in CSV")
        return

    conn = psycopg2.connect(**conn_kwargs)
    conn.autocommit = False
    cur = conn.cursor()
    cur.execute(DDL)
    cur.execute("TRUNCATE TABLE rbi_odi_investments RESTART IDENTITY;")
    psycopg2.extras.execute_values(
        cur,
        f"INSERT INTO rbi_odi_investments ({','.join(INSERT_COLS)}) VALUES %s",
        rows, page_size=1000,
    )
    n_ins = cur.rowcount
    conn.commit()
    cur.execute("SELECT COUNT(*) FROM rbi_odi_investments;")
    n_after = cur.fetchone()[0]
    cur.execute("SELECT COUNT(DISTINCT indian_party) FROM rbi_odi_investments;")
    n_parties = cur.fetchone()[0]
    cur.execute("SELECT COUNT(DISTINCT country) FROM rbi_odi_investments;")
    n_countries = cur.fetchone()[0]
    cur.execute("SELECT MIN(period_from), MAX(period_to) FROM rbi_odi_investments;")
    pmin, pmax = cur.fetchone()
    print(f"[{label}] inserted={n_ins:,} rows; "
          f"parties={n_parties:,} countries={n_countries} "
          f"period={pmin}..{pmax}")
    conn.close()


if __name__ == "__main__":
    targets = sys.argv[1:] or ["local", "rds"]
    if "local" in targets:
        load_into("local", LOCAL)
    if "rds" in targets:
        load_into("RDS  ", RDS)
