#!/usr/bin/env python3
"""Load 5 scraped MCA + 3 opensanctions CSVs to RDS via streaming COPY.

Used after laptop->RDS direct loads were timing out. Run from EC2 (same VPC
as RDS). Streams the per-row source_id rewrite to a tempfile rather than
materialising a big StringIO — important on the t4g.micro EC2 (3.7GB RAM).
"""
import csv, os, sys, time, tempfile
import psycopg2

DB = dict(host="overwatch-aml.cnsgg0i0cxa7.ap-south-1.rds.amazonaws.com",
          user="aayush", password="Aaayyuusshhh", dbname="risk_pipeline",
          connect_timeout=60,
          keepalives=1, keepalives_idle=15, keepalives_interval=5,
          keepalives_count=10,
          options="-c statement_timeout=1800000")

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA = os.path.join(PROJECT_ROOT, "data")

CSVS = [
    ("mca_directors_struck_off_248",   "mca_directors_struck_off_248.csv"),
    ("mca_notice_strike_off_stk7",     "mca_notice_strike_off_stk7.csv"),
    ("mca_public_notices_stk5",        "mca_public_notices_stk5.csv"),
    ("mca_llp_strike_off_rule37",      "mca_llp_strike_off_rule37.csv"),
    ("mca_rd_compounding_orders",      "mca_rd_compounding_orders.csv"),
    ("opensanctions_crime",            "opensanctions_crime.csv"),
    ("opensanctions_peps",             "opensanctions_peps.csv"),
    ("opensanctions_debarment",        "opensanctions_debarment.csv"),
]

COLS = ["source_id","source_agency","source_list","case_unit","name","father_name",
        "date_of_birth","gender","address","reward_amount","details","has_document",
        "document_url","detail_page_url","interpol_notice_id","link_kind","scraped_at",
        "enrichment_status"]


def _db_count(conn, sid):
    with conn.cursor() as cur:
        cur.execute("SELECT COUNT(*) FROM watchlist_records WHERE source_id=%s;",
                    (sid,))
        return int(cur.fetchone()[0])


def reopen():
    return psycopg2.connect(**DB)


def load_one(conn, sid, path):
    if not os.path.exists(path):
        print(f"  [{sid}] SKIP - missing {path}", flush=True)
        return 0, conn
    t0 = time.time()
    # Stream-rewrite the CSV with source_id prepended into a tempfile,
    # then COPY from the tempfile (constant memory, regardless of CSV size).
    tmpf = tempfile.NamedTemporaryFile(mode="w", suffix=".csv",
                                       encoding="utf-8", newline="",
                                       delete=False)
    n = 0
    try:
        with open(path, encoding="utf-8", errors="replace", newline="") as src:
            rdr = csv.DictReader(src)
            w = csv.writer(tmpf)
            for r in rdr:
                w.writerow([sid] + [(r.get(c, "") or "") for c in COLS[1:]])
                n += 1
        tmpf.close()
        sz = os.path.getsize(tmpf.name) / 1024 / 1024
        print(f"  [{sid}] tempfile ready: {n:,} rows, {sz:.1f}MB",
              flush=True)
        if n == 0:
            print(f"  [{sid}] no rows in CSV, skipping", flush=True)
            return 0, conn
        # Skip if already loaded with exact count
        pre = _db_count(conn, sid)
        if pre == n:
            print(f"  [{sid}] already loaded (db={pre:,} == csv={n:,}), skipping",
                  flush=True)
            return 0, conn
        attempt = 0
        while True:
            attempt += 1
            try:
                with conn.cursor() as cur:
                    cur.execute("DELETE FROM watchlist_records WHERE source_id=%s;",
                                (sid,))
                    deleted = cur.rowcount
                    with open(tmpf.name, encoding="utf-8") as f:
                        cur.copy_expert(
                            f"COPY watchlist_records ({','.join(COLS)}) "
                            "FROM STDIN WITH (FORMAT csv)", f)
                conn.commit()
                post = _db_count(conn, sid)
                elapsed = time.time() - t0
                print(f"  [{sid}] pre={pre:,} deleted={deleted:,} "
                      f"inserted={n:,} post={post:,} ({elapsed:.1f}s)",
                      flush=True)
                return n, conn
            except (psycopg2.OperationalError, psycopg2.InterfaceError) as e:
                print(f"  [{sid}] attempt {attempt} dropped TCP "
                      f"({str(e)[:160]}) — reconnect+retry",
                      file=sys.stderr, flush=True)
                try: conn.rollback()
                except Exception: pass
                try: conn.close()
                except Exception: pass
                while True:
                    try:
                        conn = reopen()
                        break
                    except Exception as e2:
                        print(f"  [{sid}] reconnect failed ({e2}); "
                              f"sleeping 30s",
                              file=sys.stderr, flush=True)
                        time.sleep(30)
                if attempt > 10:
                    print(f"  [{sid}] giving up after 10 attempts",
                          file=sys.stderr, flush=True)
                    return 0, conn
    finally:
        try: os.unlink(tmpf.name)
        except Exception: pass


def main():
    conn = psycopg2.connect(**DB)
    conn.autocommit = False
    total = 0
    for sid, fname in CSVS:
        path = os.path.join(DATA, fname)
        try:
            n, conn = load_one(conn, sid, path)
            total += n
        except Exception as e:
            print(f"  [{sid}] FATAL: {e}", file=sys.stderr, flush=True)
            try: conn.rollback()
            except: pass
            try: conn.close()
            except: pass
            time.sleep(15)
            conn = reopen()
    conn.close()
    print(f"\nTOTAL inserted: {total:,}", flush=True)


if __name__ == "__main__":
    sys.exit(main())
