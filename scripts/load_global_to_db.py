"""Load OpenSanctions + FATF CSVs into local + RDS."""
import csv
import json
import os
import sys
import time
import psycopg2
import psycopg2.extras

csv.field_size_limit(sys.maxsize)

SCHEMA = ['source_id', 'source_agency', 'source_list', 'case_unit', 'name', 'father_name',
          'date_of_birth', 'gender', 'address', 'reward_amount', 'details', 'has_document',
          'document_url', 'detail_page_url', 'interpol_notice_id', 'link_kind', 'scraped_at',
          'enrichment_status']

LOCAL = dict(host='localhost', user='aayush', password='aayush123', dbname='risk_pipeline')
RDS = dict(host='overwatch-aml.cnsgg0i0cxa7.ap-south-1.rds.amazonaws.com',
           user='aayush', password='Aaayyuusshhh', dbname='risk_pipeline',
           connect_timeout=60, keepalives=1, keepalives_idle=30,
           keepalives_interval=10, keepalives_count=5)


def insert_rows_to_db(sid, rows):
    if not rows:
        print(f"    {sid}: no rows, skipping")
        return
    print(f"\n  === {sid}: {len(rows):,} rows ===")
    cols = ",".join(SCHEMA)
    for label, kwargs in [("LOCAL", LOCAL), ("RDS", RDS)]:
        t0 = time.time()
        try:
            conn = psycopg2.connect(**kwargs)
            cur = conn.cursor()

            cur.execute("SELECT COUNT(*) FROM watchlist_records WHERE source_id=%s;", (sid,))
            before = cur.fetchone()[0]

            cur.execute("DELETE FROM watchlist_records WHERE source_id=%s;", (sid,))
            deleted = cur.rowcount
            conn.commit()

            inserted = 0
            chunk = 2000 if label == "RDS" else 5000
            for i in range(0, len(rows), chunk):
                batch = rows[i:i + chunk]
                psycopg2.extras.execute_values(
                    cur,
                    f"INSERT INTO watchlist_records ({cols}) VALUES %s",
                    batch,
                    page_size=chunk,
                )
                conn.commit()
                inserted += len(batch)
                if label == "RDS" and i and i % (chunk * 25) == 0:
                    elapsed = time.time() - t0
                    print(f"    [{label}] ... {inserted:,}/{len(rows):,} ({elapsed:.0f}s)")

            cur.execute("SELECT COUNT(*) FROM watchlist_records WHERE source_id=%s;", (sid,))
            after = cur.fetchone()[0]
            print(f"    [{label}] {before:,} -> {after:,} (deleted={deleted:,}, "
                  f"inserted={inserted:,}, {time.time()-t0:.0f}s)")
            conn.close()
        except Exception as e:
            print(f"    [{label}] ERROR: {e}")


def load_simple_csv(sid, csv_path):
    """Load a CSV whose every row maps to one source_id."""
    if not os.path.exists(csv_path):
        print(f"  MISSING: {csv_path}")
        return
    rows = []
    with open(csv_path, encoding='utf-8') as f:
        for r in csv.DictReader(f):
            rows.append(tuple(sid if c == 'source_id' else (r.get(c, '') or '') for c in SCHEMA))
    insert_rows_to_db(sid, rows)


def load_fatf_csv(csv_path, pair_map):
    """FATF CSV has two source_lists in one file - split by (agency,list)."""
    if not os.path.exists(csv_path):
        print(f"  MISSING: {csv_path}")
        return
    by_sid = {}
    unmapped = 0
    with open(csv_path, encoding='utf-8') as f:
        for r in csv.DictReader(f):
            sid = pair_map.get((r['source_agency'], r['source_list']))
            if not sid:
                unmapped += 1
                continue
            by_sid.setdefault(sid, []).append(
                tuple(sid if c == 'source_id' else (r.get(c, '') or '') for c in SCHEMA)
            )
    if unmapped:
        print(f"  FATF unmapped rows: {unmapped}")
    for sid, rows in by_sid.items():
        insert_rows_to_db(sid, rows)


if __name__ == "__main__":
    os.chdir(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

    SRC = json.load(open('sources.json'))
    pair_map = {(s.get('agency'), s.get('list_name')): s['id']
                for s in SRC['sources'] if s.get('agency') and s.get('list_name')}

    for sid, path in [
        ("opensanctions_debarment", "data/opensanctions_debarment.csv"),
        ("opensanctions_crime", "data/opensanctions_crime.csv"),
        ("opensanctions_peps", "data/opensanctions_peps.csv"),
    ]:
        load_simple_csv(sid, path)

    load_fatf_csv("data/fatf_lists.csv", pair_map)
    print("\n=== Done loading global sources ===")
