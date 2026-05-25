#!/usr/bin/env python3
"""Fix the OFDIM220611.xls misattribution in rbi_odi_investments.

This Excel file is a CUMULATIVE historical workbook with 47 sheets covering
July 2007 through May 2011 - one sheet per month. The original parser only
captured the first sheet's name ('July_2007') as the period for all 13,621
rows, and the period_from regex (which required colons) couldn't extract
the per-sheet 'FROM 01/07/2007 TO 31/07/2007' header.

This script:
1. Deletes all rows where excel_filename = 'OFDIM220611.xls'
2. Re-parses the file with the patched parser (per-sheet period name +
   loosened From/To regex)
3. Re-inserts the rows with correct period / period_from / period_to per sheet
"""
import os
import sys
from datetime import datetime, timezone

import psycopg2
import psycopg2.extras

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from scrapers.rbi_odi_investments import parse_excel

PROJECT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
EXCEL_PATH = os.path.join(PROJECT, "data", "rbi_odi", "OFDIM220611.xls")
EXCEL_FILENAME = "OFDIM220611.xls"

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


def fix(label, conn_kwargs, prid, press_url, excel_url):
    conn = psycopg2.connect(**conn_kwargs)
    cur = conn.cursor()

    cur.execute(
        "DELETE FROM rbi_odi_investments WHERE excel_filename = %s;",
        (EXCEL_FILENAME,),
    )
    deleted = cur.rowcount

    _, rows = parse_excel(EXCEL_PATH)
    scraped_at = datetime.now(timezone.utc).isoformat(timespec="seconds")

    by_period = {}
    insert_tuples = []
    for r in rows:
        period = r.get("period") or "(unknown)"
        by_period[period] = by_period.get(period, 0) + 1
        insert_tuples.append((
            prid, press_url, excel_url, EXCEL_FILENAME,
            period, r.get("period_from"), r.get("period_to"),
            r.get("sr_no"), r["indian_party"], r.get("jv_wos_name"),
            r.get("jv_or_wos"), r.get("country"), r.get("activity"),
            r.get("equity_usd_mn", 0.0), r.get("loan_usd_mn", 0.0),
            r.get("guarantee_usd_mn", 0.0), r.get("total_usd_mn", 0.0),
            scraped_at,
        ))

    psycopg2.extras.execute_values(
        cur,
        f"INSERT INTO rbi_odi_investments ({','.join(INSERT_COLS)}) VALUES %s",
        insert_tuples, page_size=1000,
    )
    conn.commit()

    print(f"[{label}] deleted={deleted:,}, re-inserted={len(insert_tuples):,}")
    print(f"  per-sheet row counts:")
    for sheet, n in sorted(by_period.items()):
        print(f"    {sheet:15s} -> {n:4d} rows")

    cur.execute(
        "SELECT COUNT(*) FROM rbi_odi_investments "
        "WHERE excel_filename = %s AND (period_from IS NULL OR period_from = '');",
        (EXCEL_FILENAME,),
    )
    blank_left = cur.fetchone()[0]
    print(f"  remaining blank period_from for this file: {blank_left}")

    conn.close()


if __name__ == "__main__":
    # Look up the original prid + press_release_url + excel_url so we can preserve them
    conn = psycopg2.connect(**LOCAL)
    cur = conn.cursor()
    cur.execute(
        "SELECT prid, press_release_url, excel_url FROM rbi_odi_investments "
        "WHERE excel_filename = %s LIMIT 1;",
        (EXCEL_FILENAME,),
    )
    row = cur.fetchone()
    conn.close()
    if not row:
        print(f"ERROR: no existing rows for {EXCEL_FILENAME} in local DB")
        sys.exit(1)
    prid, press_url, excel_url = row
    print(f"Using prid={prid} press_url={press_url} excel_url={excel_url}")

    fix("local", LOCAL, prid, press_url, excel_url)
    fix("RDS  ", RDS, prid, press_url, excel_url)
