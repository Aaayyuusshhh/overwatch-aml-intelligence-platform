"""Load pending MCA CSVs (struck_off + public_notices_stk6) to local DB and RDS."""
import csv
import os
import sys
import psycopg2
import psycopg2.extras

SCHEMA = ['source_id', 'source_agency', 'source_list', 'case_unit', 'name', 'father_name',
          'date_of_birth', 'gender', 'address', 'reward_amount', 'details', 'has_document',
          'document_url', 'detail_page_url', 'interpol_notice_id', 'link_kind', 'scraped_at',
          'enrichment_status']

LOCAL = dict(host='localhost', user='aayush', password='aayush123', dbname='risk_pipeline')
RDS = dict(host='overwatch-aml.cnsgg0i0cxa7.ap-south-1.rds.amazonaws.com',
           user='aayush', password='Aaayyuusshhh', dbname='risk_pipeline', connect_timeout=60)

# allow larger CSV fields (some details can exceed default limit)
csv.field_size_limit(sys.maxsize)


def load_csv_to_db(sid, csv_path):
    rows = []
    with open(csv_path, encoding='utf-8') as f:
        for r in csv.DictReader(f):
            rows.append(tuple(sid if c == 'source_id' else (r.get(c, '') or '') for c in SCHEMA))

    if not rows:
        print(f"  {sid}: CSV is empty, skipping")
        return

    print(f"  {sid}: {len(rows):,} rows from CSV")

    for label, kwargs in [("LOCAL", LOCAL), ("RDS", RDS)]:
        try:
            conn = psycopg2.connect(**kwargs)
            cur = conn.cursor()

            cur.execute("SELECT COUNT(*) FROM watchlist_records WHERE source_id=%s;", (sid,))
            before = cur.fetchone()[0]

            cur.execute("DELETE FROM watchlist_records WHERE source_id=%s;", (sid,))
            deleted = cur.rowcount

            cols = ",".join(SCHEMA)
            psycopg2.extras.execute_values(
                cur,
                f"INSERT INTO watchlist_records ({cols}) VALUES %s",
                rows,
                page_size=5000,
            )
            conn.commit()

            cur.execute("SELECT COUNT(*) FROM watchlist_records WHERE source_id=%s;", (sid,))
            after = cur.fetchone()[0]

            print(f"    [{label}] {before:,} -> {after:,} (deleted={deleted:,}, inserted={len(rows):,})")
            conn.close()
        except Exception as e:
            print(f"    [{label}] ERROR: {e}")


if __name__ == "__main__":
    os.chdir(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    print("=== Loading pending MCA CSVs ===")
    load_csv_to_db('mca_companies_struck_off', 'data/mca_companies_struck_off.csv')
    load_csv_to_db('mca_public_notices_stk6', 'data/mca_public_notices_stk6.csv')
    print("=== Done ===")
