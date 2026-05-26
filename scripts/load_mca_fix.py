"""Clean DELETE then INSERT for the two pending MCA CSVs.

Uses smaller batches + autocommit DELETE to avoid RDS timeouts on the larger file.
"""
import csv
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


def load(sid, csv_path):
    rows = []
    with open(csv_path, encoding='utf-8') as f:
        for r in csv.DictReader(f):
            rows.append(tuple(sid if c == 'source_id' else (r.get(c, '') or '') for c in SCHEMA))

    if not rows:
        print(f"  {sid}: CSV is empty, skipping")
        return

    print(f"\n  === {sid}: {len(rows):,} rows from CSV ===")

    for label, kwargs in [("LOCAL", LOCAL), ("RDS", RDS)]:
        t0 = time.time()
        try:
            conn = psycopg2.connect(**kwargs)
            cur = conn.cursor()

            cur.execute("SELECT COUNT(*) FROM watchlist_records WHERE source_id=%s;", (sid,))
            before = cur.fetchone()[0]
            print(f"    [{label}] before: {before:,}")

            # DELETE in its own transaction so it commits cleanly
            cur.execute("DELETE FROM watchlist_records WHERE source_id=%s;", (sid,))
            deleted = cur.rowcount
            conn.commit()
            print(f"    [{label}] deleted: {deleted:,}")

            # INSERT in chunks of 5,000 with commits per chunk - safer for RDS
            cols = ",".join(SCHEMA)
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
                if label == "RDS" and i % (chunk * 5) == 0:
                    elapsed = time.time() - t0
                    print(f"    [{label}] ... inserted {inserted:,}/{len(rows):,} ({elapsed:.0f}s)")

            cur.execute("SELECT COUNT(*) FROM watchlist_records WHERE source_id=%s;", (sid,))
            after = cur.fetchone()[0]
            print(f"    [{label}] after: {after:,} (took {time.time()-t0:.0f}s)")
            conn.close()
        except Exception as e:
            print(f"    [{label}] ERROR: {e}")


if __name__ == "__main__":
    os.chdir(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    load('mca_companies_struck_off', 'data/mca_companies_struck_off.csv')
    load('mca_public_notices_stk6', 'data/mca_public_notices_stk6.csv')
