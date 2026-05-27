"""LOCAL: load all pending sources via DELETE + COPY.

LOCAL state: indexes already mostly dropped (only PK + idx_source_id remain),
so DELETE-by-source_id is fast.

After all loads, recreate the 8 missing indexes (idx_dob, idx_name_*, idx_source*).
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

LOCAL = dict(host='localhost', user='aayush', password='aayush123', dbname='risk_pipeline')

# Indexes to recreate at the end (everything except PK and idx_source_id).
INDEX_DDLS = [
    ("idx_dob", "CREATE INDEX idx_dob ON public.watchlist_records USING btree (date_of_birth)"),
    ("idx_name_lower", "CREATE INDEX idx_name_lower ON public.watchlist_records USING btree (lower(name))"),
    ("idx_watchlist_name", "CREATE INDEX idx_watchlist_name ON public.watchlist_records USING btree (name)"),
    ("idx_watchlist_source_list", "CREATE INDEX idx_watchlist_source_list ON public.watchlist_records USING btree (source_list)"),
    ("idx_source", "CREATE INDEX idx_source ON public.watchlist_records USING btree (source_agency, source_list)"),
    ("idx_source_agency_list", "CREATE INDEX idx_source_agency_list ON public.watchlist_records USING btree (source_agency, source_list)"),
    ("idx_name_trgm", "CREATE INDEX idx_name_trgm ON public.watchlist_records USING gin (name gin_trgm_ops)"),
    ("idx_name_embedding_hnsw", "CREATE INDEX idx_name_embedding_hnsw ON public.watchlist_records USING hnsw (name_embedding vector_cosine_ops) WITH (m='16', ef_construction='64')"),
]


def log(msg):
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


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
    log(f"  {sid}: after={after:,} (del={deleted:,}, ins={inserted:,}, total {time.time()-t0:.0f}s)")


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
        log(f"  split FATF -> {sid}: {len(rows)} rows -> {p}")
        paths.append((sid, p))
    return paths


def recreate_indexes(conn):
    cur = conn.cursor()
    cur.execute("SELECT indexname FROM pg_indexes WHERE tablename='watchlist_records';")
    existing = {r[0] for r in cur.fetchall()}
    for name, ddl in INDEX_DDLS:
        if name in existing:
            log(f"  {name}: already exists, skip")
            continue
        t0 = time.time()
        try:
            cur.execute(ddl)
            conn.commit()
            log(f"  {name}: created ({time.time()-t0:.0f}s)")
        except Exception as e:
            conn.rollback()
            log(f"  {name}: FAILED: {e}")


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

    log(f"LOCAL: loading {len(sources)} sources")
    t0 = time.time()

    try:
        conn = psycopg2.connect(**LOCAL)
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
        recreate_indexes(conn)

        cur = conn.cursor()
        cur.execute("SELECT COUNT(*) FROM watchlist_records;")
        total = cur.fetchone()[0]
        cur.execute("SELECT COUNT(DISTINCT source_id) FROM watchlist_records WHERE source_id != '';")
        sources_count = cur.fetchone()[0]
        log(f"  LOCAL FINAL: {total:,} rows, {sources_count} sources, total {time.time()-t0:.0f}s")
        conn.close()
    except Exception:
        log(f"FATAL:\n{traceback.format_exc()}")


if __name__ == "__main__":
    main()
