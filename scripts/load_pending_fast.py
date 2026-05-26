"""Fast loader: batched DELETE + COPY INSERT, with LOCAL and RDS in parallel threads.

Usage:
  python scripts/load_pending_fast.py            # MCA + global
  python scripts/load_pending_fast.py mca        # MCA only
  python scripts/load_pending_fast.py global     # OpenSanctions + FATF only
"""
import csv
import io
import json
import os
import sys
import threading
import time
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

_print_lock = threading.Lock()


def log(msg):
    with _print_lock:
        print(msg, flush=True)


def batched_delete(cur, conn, sid, label, batch=20000):
    """Delete in small batches with commit per batch."""
    total = 0
    while True:
        cur.execute(
            "DELETE FROM watchlist_records WHERE ctid IN ("
            "  SELECT ctid FROM watchlist_records WHERE source_id=%s LIMIT %s"
            ")", (sid, batch))
        n = cur.rowcount
        conn.commit()
        total += n
        if n == 0:
            break
        log(f"    [{label}] {sid}: deleted batch={n:,} total={total:,}")
    return total


def copy_insert(cur, conn, sid, rows_iter, label):
    """COPY rows from in-memory TSV iterator. rows_iter yields dicts."""
    buf = io.StringIO()
    writer = csv.writer(buf, delimiter='\t', quoting=csv.QUOTE_MINIMAL,
                        quotechar='"', escapechar='\\', lineterminator='\n')
    count = 0
    for row in rows_iter:
        # row is a dict with the data CSV columns (no source_id)
        out = [sid]
        for col in SCHEMA[1:]:
            v = row.get(col, '') or ''
            # Strip tabs/newlines from cell content to avoid TSV breakage
            v = v.replace('\t', ' ').replace('\r', ' ').replace('\n', ' ')
            out.append(v)
        writer.writerow(out)
        count += 1

    buf.seek(0)
    cols = ",".join(SCHEMA)
    cur.copy_expert(
        f"COPY watchlist_records ({cols}) FROM STDIN WITH (FORMAT csv, DELIMITER E'\\t', QUOTE '\"', ESCAPE '\\')",
        buf,
    )
    conn.commit()
    log(f"    [{label}] {sid}: COPYed {count:,} rows")
    return count


def load_csv_for_label(sid, csv_path, label, kwargs):
    """Connect to one DB, batched-delete + COPY-insert for one source."""
    t0 = time.time()
    try:
        conn = psycopg2.connect(**kwargs)
        conn.autocommit = False
        cur = conn.cursor()

        cur.execute("SELECT COUNT(*) FROM watchlist_records WHERE source_id=%s;", (sid,))
        before = cur.fetchone()[0]
        log(f"  [{label}] {sid}: before={before:,}")

        deleted = batched_delete(cur, conn, sid, label, batch=20000)

        with open(csv_path, encoding='utf-8') as f:
            reader = csv.DictReader(f)
            inserted = copy_insert(cur, conn, sid, reader, label)

        cur.execute("SELECT COUNT(*) FROM watchlist_records WHERE source_id=%s;", (sid,))
        after = cur.fetchone()[0]
        elapsed = time.time() - t0
        log(f"  [{label}] {sid}: {before:,} -> {after:,} "
            f"(del={deleted:,}, ins={inserted:,}, {elapsed:.0f}s)")
        conn.close()
    except Exception as e:
        log(f"  [{label}] {sid}: ERROR: {e}")


def load_source_parallel(sid, csv_path):
    """Process one source on LOCAL and RDS concurrently."""
    if not os.path.exists(csv_path):
        log(f"  MISSING: {csv_path}")
        return
    log(f"\n=== {sid} ({csv_path}) ===")
    t0 = time.time()
    t_local = threading.Thread(target=load_csv_for_label, args=(sid, csv_path, "LOCAL", LOCAL))
    t_rds = threading.Thread(target=load_csv_for_label, args=(sid, csv_path, "RDS", RDS))
    t_local.start()
    t_rds.start()
    t_local.join()
    t_rds.join()
    log(f"  === {sid} done in {time.time()-t0:.0f}s ===")


def load_fatf_parallel(csv_path, pair_map):
    """FATF CSV has two source_lists - split, load each on LOCAL + RDS in parallel."""
    if not os.path.exists(csv_path):
        log(f"  MISSING: {csv_path}")
        return
    by_sid = {}
    with open(csv_path, encoding='utf-8') as f:
        for r in csv.DictReader(f):
            sid = pair_map.get((r['source_agency'], r['source_list']))
            if sid:
                by_sid.setdefault(sid, []).append(r)

    # Write temp per-sid CSVs and load
    tmp_dir = "data/_fatf_tmp"
    os.makedirs(tmp_dir, exist_ok=True)
    for sid, rows in by_sid.items():
        tmp_path = f"{tmp_dir}/{sid}.csv"
        fields = [c for c in SCHEMA if c != 'source_id']
        with open(tmp_path, 'w', newline='', encoding='utf-8') as f:
            w = csv.DictWriter(f, fieldnames=fields)
            w.writeheader()
            for r in rows:
                w.writerow({c: r.get(c, '') for c in fields})
        load_source_parallel(sid, tmp_path)


def main():
    os.chdir(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    target = sys.argv[1] if len(sys.argv) > 1 else "all"

    if target in ("all", "mca"):
        log("\n############ MCA ############")
        load_source_parallel('mca_companies_struck_off', 'data/mca_companies_struck_off.csv')
        load_source_parallel('mca_public_notices_stk6', 'data/mca_public_notices_stk6.csv')

    if target in ("all", "global"):
        log("\n############ Global (OpenSanctions + FATF) ############")
        SRC = json.load(open('sources.json'))
        pair_map = {(s.get('agency'), s.get('list_name')): s['id']
                    for s in SRC['sources'] if s.get('agency') and s.get('list_name')}

        for sid, path in [
            ("opensanctions_debarment", "data/opensanctions_debarment.csv"),
            ("opensanctions_crime", "data/opensanctions_crime.csv"),
            ("opensanctions_peps", "data/opensanctions_peps.csv"),
        ]:
            load_source_parallel(sid, path)

        load_fatf_parallel("data/fatf_lists.csv", pair_map)

    log("\n=== Done ===")


if __name__ == "__main__":
    main()
