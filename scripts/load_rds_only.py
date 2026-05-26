"""Load all sources into RDS only.

RDS-specific strategy:
- Connection with aggressive keepalives to survive AWS NLB idle timeouts
- Drop only the heavy non-essential indexes (HNSW, GIN trgm); keep btree(source_id)
  so DELETEs can use it and finish quickly.
- COPY-insert per source.
- Recreate the dropped indexes.
"""
import csv
import io
import json
import os
import sys
import time
import traceback
import psycopg2

csv.field_size_limit(sys.maxsize)

SCHEMA = ['source_id', 'source_agency', 'source_list', 'case_unit', 'name', 'father_name',
          'date_of_birth', 'gender', 'address', 'reward_amount', 'details', 'has_document',
          'document_url', 'detail_page_url', 'interpol_notice_id', 'link_kind', 'scraped_at',
          'enrichment_status']

RDS = dict(host='overwatch-aml.cnsgg0i0cxa7.ap-south-1.rds.amazonaws.com',
           user='aayush', password='Aaayyuusshhh', dbname='risk_pipeline',
           connect_timeout=60, keepalives=1, keepalives_idle=15,
           keepalives_interval=5, keepalives_count=10,
           options="-c statement_timeout=0 -c tcp_user_timeout=60000")

# Drop these heavy indexes during bulk load (recreate after)
DROP_DURING_LOAD = [
    "idx_name_embedding_hnsw",  # 730MB HNSW vector index
    "idx_name_trgm",            # GIN trigram on name
    "idx_name_lower",           # btree on lower(name)
]


def log(msg):
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def fetch_index_ddls(cur, names):
    cur.execute(
        "SELECT indexname, indexdef FROM pg_indexes "
        "WHERE tablename='watchlist_records' AND indexname = ANY(%s);",
        (names,),
    )
    return cur.fetchall()


def drop_indexes(conn, names):
    cur = conn.cursor()
    for n in names:
        t0 = time.time()
        cur.execute(f"DROP INDEX IF EXISTS {n};")
        conn.commit()
        log(f"  dropped {n} ({time.time()-t0:.1f}s)")


def recreate_indexes(conn, ddls):
    cur = conn.cursor()
    for name, ddl in ddls:
        t0 = time.time()
        try:
            cur.execute(ddl)
            conn.commit()
            log(f"  created {name} ({time.time()-t0:.0f}s)")
        except Exception as e:
            conn.rollback()
            log(f"  FAILED to create {name}: {e}")


def copy_csv(cur, conn, sid, csv_path):
    buf = io.StringIO()
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
    log(f"  {sid}: COPY {count:,} rows ({time.time()-t0:.0f}s)")
    return count


def load_source(conn, sid, csv_path):
    cur = conn.cursor()
    t0 = time.time()
    cur.execute("SELECT COUNT(*) FROM watchlist_records WHERE source_id=%s;", (sid,))
    before = cur.fetchone()[0]
    log(f"  {sid}: before={before:,}")

    cur.execute("DELETE FROM watchlist_records WHERE source_id=%s;", (sid,))
    deleted = cur.rowcount
    conn.commit()
    log(f"  {sid}: DELETE {deleted:,} ({time.time()-t0:.0f}s)")

    inserted = copy_csv(cur, conn, sid, csv_path)

    cur.execute("SELECT COUNT(*) FROM watchlist_records WHERE source_id=%s;", (sid,))
    after = cur.fetchone()[0]
    log(f"  {sid}: after={after:,} (del={deleted:,}, ins={inserted:,}, "
        f"total {time.time()-t0:.0f}s)")


def split_fatf(csv_path, pair_map, out_dir="data/_fatf_tmp"):
    os.makedirs(out_dir, exist_ok=True)
    by_sid = {}
    with open(csv_path, encoding='utf-8') as f:
        for r in csv.DictReader(f):
            sid = pair_map.get((r['source_agency'], r['source_list']))
            if sid:
                by_sid.setdefault(sid, []).append(r)
    fields = [c for c in SCHEMA if c != 'source_id']
    paths = []
    for sid, rows in by_sid.items():
        p = f"{out_dir}/{sid}.csv"
        with open(p, 'w', newline='', encoding='utf-8') as f:
            w = csv.DictWriter(f, fieldnames=fields)
            w.writeheader()
            for r in rows:
                w.writerow({c: r.get(c, '') for c in fields})
        paths.append((sid, p))
    return paths


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

    log(f"RDS: loading {len(sources)} sources")
    t0 = time.time()
    ddls = []

    try:
        conn = psycopg2.connect(**RDS)
        cur = conn.cursor()

        ddls = fetch_index_ddls(cur, DROP_DURING_LOAD)
        log(f"  captured {len(ddls)} indexes to drop")
        log("  === DROP heavy indexes ===")
        drop_indexes(conn, [n for n, _ in ddls])

        log("  === LOAD SOURCES ===")
        for sid, path in sources:
            if not os.path.exists(path):
                log(f"  MISSING: {path}")
                continue
            try:
                load_source(conn, sid, path)
            except Exception as e:
                log(f"  {sid}: ERROR: {e}")
                conn.rollback()

        log("  === RECREATE indexes ===")
        recreate_indexes(conn, ddls)

        cur.execute("SELECT COUNT(*) FROM watchlist_records;")
        total = cur.fetchone()[0]
        log(f"  RDS FINAL: {total:,} rows, total {time.time()-t0:.0f}s")
        conn.close()
    except Exception:
        log(f"FATAL:\n{traceback.format_exc()}")
        if ddls:
            try:
                conn = psycopg2.connect(**RDS)
                log("  recovery: recreating indexes")
                recreate_indexes(conn, ddls)
                conn.close()
            except Exception as e:
                log(f"  recovery failed: {e}")


if __name__ == "__main__":
    main()
