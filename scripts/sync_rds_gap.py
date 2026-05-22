#!/usr/bin/env python3
"""One-off: Sync 3 mismatched (source_agency, source_list) pairs from local DB to RDS.

Background: a load_to_db refresh ran locally for these three sources and shrank
the row counts; RDS still has the old (larger) row counts. Local is the source
of truth — DELETE + COPY those three pairs into RDS to close the 10-row gap.
"""
import io, csv, time
import psycopg2

LOCAL = dict(host="localhost", user="aayush", password="aayush123", dbname="risk_pipeline")
RDS = dict(
    host="overwatch-aml.cnsgg0i0cxa7.ap-south-1.rds.amazonaws.com",
    user="aayush", password="Aaayyuusshhh", dbname="risk_pipeline",
    connect_timeout=30,
)

PAIRS = [
    ("CMA", "Decisions"),
    ("CMA", "Decisions (Kenya)"),
    ("Enforcement Directorate (ED)", "Press Releases"),
]

COLS = ["source_id", "source_agency", "source_list", "case_unit", "name",
        "father_name", "date_of_birth", "gender", "address", "reward_amount",
        "details", "has_document", "document_url", "detail_page_url",
        "interpol_notice_id", "link_kind", "scraped_at", "enrichment_status"]
COL_LIST = ", ".join(COLS)


def main():
    lconn = psycopg2.connect(**LOCAL)
    lcur = lconn.cursor()
    rconn = psycopg2.connect(**RDS)
    rconn.autocommit = True
    rcur = rconn.cursor()

    for ag, ls in PAIRS:
        t0 = time.time()
        lcur.execute(
            f"SELECT {COL_LIST} FROM watchlist_records "
            "WHERE source_agency = %s AND source_list = %s",
            (ag, ls),
        )
        rows = lcur.fetchall()
        rcur.execute(
            "DELETE FROM watchlist_records "
            "WHERE source_agency = %s AND source_list = %s",
            (ag, ls),
        )
        deleted = rcur.rowcount
        buf = io.StringIO()
        w = csv.writer(buf, quoting=csv.QUOTE_MINIMAL)
        for r in rows:
            w.writerow(["" if v is None else v for v in r])
        buf.seek(0)
        rcur.copy_expert(
            f"COPY watchlist_records ({COL_LIST}) FROM STDIN WITH CSV",
            buf,
        )
        print(f"  [{ag} / {ls}]  RDS deleted {deleted}, inserted {len(rows)}  "
              f"({time.time()-t0:.1f}s)")

    rcur.execute("SELECT COUNT(*) FROM watchlist_records")
    print(f"RDS total:   {rcur.fetchone()[0]:,}")
    lcur.execute("SELECT COUNT(*) FROM watchlist_records")
    print(f"Local total: {lcur.fetchone()[0]:,}")
    lconn.close(); rconn.close()


if __name__ == "__main__":
    main()
