"""Fast bulk loader with index drop/recreate.

For each DB (LOCAL + RDS, in parallel threads):
  1. Capture and drop all non-PK indexes on watchlist_records
  2. For each source: DELETE old rows (fast w/o indexes), COPY new rows
  3. Recreate all indexes

WARNING: while indexes are dropped, queries against the table are slow.
Any concurrent scraper (mca_rd_roc.py) may suffer briefly.
"""
import csv
import io
import json
import os
import sys
import threading
import time
import traceback
import psycopg2

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

_lock = threading.Lock()


def log(msg):
    with _lock:
        ts = time.strftime("%H:%M:%S")
        print(f"[{ts}] {msg}", flush=True)


def capture_indexes(cur):
    cur.execute(
        "SELECT indexname, indexdef FROM pg_indexes "
        "WHERE tablename='watchlist_records' AND indexname != 'watchlist_records_pkey' "
        "ORDER BY indexname;"
    )
    return cur.fetchall()


def drop_indexes(conn, label, indexes):
    cur = conn.cursor()
    for name, _ddl in indexes:
        t0 = time.time()
        cur.execute(f"DROP INDEX IF EXISTS {name};")
        conn.commit()
        log(f"  [{label}] dropped {name} ({time.time()-t0:.1f}s)")


def recreate_indexes(conn, label, indexes):
    cur = conn.cursor()
    for name, ddl in indexes:
        t0 = time.time()
        try:
            cur.execute(ddl)
            conn.commit()
            log(f"  [{label}] created {name} ({time.time()-t0:.0f}s)")
        except Exception as e:
            conn.rollback()
            log(f"  [{label}] FAILED to create {name}: {e}")


def copy_csv_rows(cur, conn, sid, csv_path, label):
    """Stream rows from CSV through StringIO and COPY into table."""
    buf = io.StringIO()
    # csv.writer defaults: doubles embedded " inside quoted fields -- matches PG COPY's QUOTE '"' ESCAPE '"'
    writer = csv.writer(buf, delimiter='\t', quoting=csv.QUOTE_MINIMAL,
                        quotechar='"', lineterminator='\n')
    count = 0
    with open(csv_path, encoding='utf-8') as f:
        reader = csv.DictReader(f)
        for r in reader:
            out = [sid]
            for col in SCHEMA[1:]:
                v = r.get(col, '') or ''
                v = v.replace('\t', ' ').replace('\r', ' ').replace('\n', ' ')
                out.append(v)
            writer.writerow(out)
            count += 1

    buf.seek(0)
    cols = ",".join(SCHEMA)
    t0 = time.time()
    cur.copy_expert(
        f"COPY watchlist_records ({cols}) FROM STDIN WITH (FORMAT csv, DELIMITER E'\\t', QUOTE '\"', ESCAPE '\"')",
        buf,
    )
    conn.commit()
    log(f"  [{label}] {sid}: COPY {count:,} rows ({time.time()-t0:.0f}s)")
    return count


def load_source(conn, sid, csv_path, label):
    cur = conn.cursor()
    t0 = time.time()
    cur.execute("SELECT COUNT(*) FROM watchlist_records WHERE source_id=%s;", (sid,))
    before = cur.fetchone()[0]
    log(f"  [{label}] {sid}: before={before:,}")

    cur.execute("DELETE FROM watchlist_records WHERE source_id=%s;", (sid,))
    deleted = cur.rowcount
    conn.commit()
    log(f"  [{label}] {sid}: DELETE {deleted:,} ({time.time()-t0:.0f}s)")

    inserted = copy_csv_rows(cur, conn, sid, csv_path, label)

    cur.execute("SELECT COUNT(*) FROM watchlist_records WHERE source_id=%s;", (sid,))
    after = cur.fetchone()[0]
    log(f"  [{label}] {sid}: after={after:,} (del={deleted:,}, ins={inserted:,}, "
        f"total {time.time()-t0:.0f}s)")


def split_fatf(csv_path, pair_map, out_dir="data/_fatf_tmp"):
    """Split fatf_lists.csv into per-sid temp CSVs."""
    os.makedirs(out_dir, exist_ok=True)
    by_sid = {}
    with open(csv_path, encoding='utf-8') as f:
        for r in csv.DictReader(f):
            sid = pair_map.get((r['source_agency'], r['source_list']))
            if sid:
                by_sid.setdefault(sid, []).append(r)
    fields = [c for c in SCHEMA if c != 'source_id']
    out_paths = []
    for sid, rows in by_sid.items():
        out_path = f"{out_dir}/{sid}.csv"
        with open(out_path, 'w', newline='', encoding='utf-8') as f:
            w = csv.DictWriter(f, fieldnames=fields)
            w.writeheader()
            for r in rows:
                w.writerow({c: r.get(c, '') for c in fields})
        out_paths.append((sid, out_path))
        log(f"  split FATF -> {sid}: {len(rows)} rows -> {out_path}")
    return out_paths


def do_db(label, kwargs, sources):
    """Per-DB: drop indexes, load all sources, recreate indexes."""
    t0 = time.time()
    indexes = None
    try:
        conn = psycopg2.connect(**kwargs)
        cur = conn.cursor()
        indexes = capture_indexes(cur)
        log(f"  [{label}] captured {len(indexes)} indexes")

        log(f"  [{label}] === DROP INDEXES ===")
        drop_indexes(conn, label, indexes)

        log(f"  [{label}] === LOAD SOURCES ===")
        for sid, csv_path in sources:
            if not os.path.exists(csv_path):
                log(f"  [{label}] MISSING: {csv_path}")
                continue
            try:
                load_source(conn, sid, csv_path, label)
            except Exception as e:
                log(f"  [{label}] {sid}: ERROR: {e}")
                conn.rollback()

        log(f"  [{label}] === RECREATE INDEXES ===")
        recreate_indexes(conn, label, indexes)

        cur.execute("SELECT COUNT(*) FROM watchlist_records;")
        total = cur.fetchone()[0]
        log(f"  [{label}] FINAL: {total:,} total rows, {time.time()-t0:.0f}s")
        conn.close()
    except Exception:
        log(f"  [{label}] FATAL:\n{traceback.format_exc()}")
        if indexes:
            try:
                conn = psycopg2.connect(**kwargs)
                log(f"  [{label}] attempting index recreate after failure")
                recreate_indexes(conn, label, indexes)
                conn.close()
            except Exception as e:
                log(f"  [{label}] index recreate also failed: {e}")


def main():
    os.chdir(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

    SRC = json.load(open('sources.json'))
    pair_map = {(s.get('agency'), s.get('list_name')): s['id']
                for s in SRC['sources'] if s.get('agency') and s.get('list_name')}

    sources = [
        ("mca_companies_struck_off", "data/mca_companies_struck_off.csv"),
        ("mca_public_notices_stk6", "data/mca_public_notices_stk6.csv"),
        ("opensanctions_debarment", "data/opensanctions_debarment.csv"),
        ("opensanctions_crime", "data/opensanctions_crime.csv"),
        ("opensanctions_peps", "data/opensanctions_peps.csv"),
    ]
    sources.extend(split_fatf("data/fatf_lists.csv", pair_map))

    log(f"Loading {len(sources)} sources on LOCAL + RDS in parallel\n")
    t0 = time.time()

    t_local = threading.Thread(target=do_db, args=("LOCAL", LOCAL, sources), name="LOCAL")
    t_rds = threading.Thread(target=do_db, args=("RDS", RDS, sources), name="RDS")
    t_local.start()
    t_rds.start()
    t_local.join()
    t_rds.join()

    log(f"\n=== ALL DONE in {time.time()-t0:.0f}s ===")


if __name__ == "__main__":
    main()
