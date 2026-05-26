#!/usr/bin/env python3
"""Load the new/refreshed MCA CSVs into local PG and AWS RDS.

For each (source_id, csv_path) pair:
  - DELETE existing rows for that source_id (clean slate)
  - INSERT all CSV rows (in batches)
  - Report before/after counts
"""
import csv, os, sys, psycopg2, psycopg2.extras

SCHEMA = ['source_id','source_agency','source_list','case_unit','name','father_name',
          'date_of_birth','gender','address','reward_amount','details','has_document',
          'document_url','detail_page_url','interpol_notice_id','link_kind','scraped_at','enrichment_status']

JOBS = [
    ('mca_roc_adjudication_orders', 'data/mca_roc_adjudication_orders.csv'),
    ('mca_llps_strike_off',         'data/mca_llps_strike_off.csv'),
    ('mca_companies_struck_off',    'data/mca_companies_struck_off.csv'),
    ('mca_disqualified_directors_164', 'data/mca_disqualified_directors_164.csv'),
    ('mca_public_notices_stk6',     'data/mca_public_notices_stk6.csv'),
    ('mca_proclaimed_offenders',    'data/mca_proclaimed_offenders.csv'),
    ('mca_defaulter_companies',     'data/mca_defaulter_companies.csv'),
    ('mca_defaulter_directors',     'data/mca_defaulter_directors.csv'),
    ('mca_dormant_companies',       'data/mca_dormant_companies.csv'),
    ('mca_vanishing_companies',     'data/mca_vanishing_companies.csv'),
    ('mca_corporate_fraud_chit_fund', 'data/mca_corporate_fraud_chit_fund.csv'),
    ('mca_dormant_companies_45',    'data/mca_dormant_companies_45.csv'),
]

TARGETS = [
    ('local', dict(host='localhost', user='aayush', password='aayush123', dbname='risk_pipeline')),
    ('RDS',   dict(host='overwatch-aml.cnsgg0i0cxa7.ap-south-1.rds.amazonaws.com', user='aayush', password='Aaayyuusshhh', dbname='risk_pipeline', connect_timeout=30)),
]

def load_csv(path, sid):
    rows = []
    with open(path) as f:
        for r in csv.DictReader(f):
            rows.append(tuple(sid if c=='source_id' else r.get(c,'') for c in SCHEMA))
    return rows

def main():
    only = set(sys.argv[1:]) if len(sys.argv) > 1 else None
    for label, kwargs in TARGETS:
        print(f"\n=== {label} ===")
        try:
            conn = psycopg2.connect(**kwargs)
        except Exception as e:
            print(f"  CONNECT FAIL: {e}")
            continue
        conn.autocommit = False
        cur = conn.cursor()
        for sid, path in JOBS:
            if only and sid not in only:
                continue
            if not os.path.exists(path):
                print(f"  [{sid}] CSV missing: {path}")
                continue
            cur.execute("SELECT COUNT(*) FROM watchlist_records WHERE source_id=%s;", (sid,))
            before = cur.fetchone()[0]
            rows = load_csv(path, sid)
            if not rows:
                print(f"  [{sid}] empty CSV; before={before}")
                continue
            cur.execute("DELETE FROM watchlist_records WHERE source_id=%s;", (sid,))
            deleted = cur.rowcount
            cols = ",".join(SCHEMA)
            psycopg2.extras.execute_values(
                cur, f"INSERT INTO watchlist_records ({cols}) VALUES %s",
                rows, page_size=500)
            conn.commit()
            cur.execute("SELECT COUNT(*) FROM watchlist_records WHERE source_id=%s;", (sid,))
            after = cur.fetchone()[0]
            print(f"  {sid:40s} {before:>7d} -> {after:>7d}  (deleted={deleted}, inserted={len(rows)})")
        cur.execute("SELECT COUNT(*) FROM watchlist_records;")
        tot = cur.fetchone()[0]
        print(f"  TOTAL records in {label}: {tot:,}")
        conn.close()

if __name__ == '__main__':
    main()
