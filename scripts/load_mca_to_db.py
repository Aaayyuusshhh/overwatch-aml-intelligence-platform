#!/usr/bin/env python3
"""Load MCA CSVs into local DB + RDS via targeted insert."""
import csv, os, sys
import psycopg2
import psycopg2.extras

PROJECT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

LOCAL = dict(host="localhost", user="aayush", password="aayush123", dbname="risk_pipeline")
RDS = dict(
    host="overwatch-aml.cnsgg0i0cxa7.ap-south-1.rds.amazonaws.com",
    user="aayush", password="Aaayyuusshhh", dbname="risk_pipeline",
    connect_timeout=30,
)

SCHEMA = ["source_id", "source_agency", "source_list", "case_unit", "name",
          "father_name", "date_of_birth", "gender", "address", "reward_amount",
          "details", "has_document", "document_url", "detail_page_url",
          "interpol_notice_id", "link_kind", "scraped_at", "enrichment_status"]

SOURCES = [
    ("mca_disqualified_directors_164",
     "Ministry of Corporate Affairs (MCA)",
     "Disqualified Directors U/S 164(2)(A)",
     os.path.join(PROJECT, "data/mca_disqualified_directors_164.csv")),
    ("mca_companies_struck_off",
     "Ministry of Corporate Affairs (MCA)",
     "Companies Struck Off (STK-7) U/S 248(5)",
     os.path.join(PROJECT, "data/mca_companies_struck_off.csv")),
]


def load_into(label, conn_kwargs):
    conn = psycopg2.connect(**conn_kwargs)
    conn.autocommit = False
    cur = conn.cursor()
    total_inserted = 0
    for source_id, agency, list_name, csv_path in SOURCES:
        if not os.path.exists(csv_path):
            print(f"  [{label}] {source_id}: CSV missing, skip")
            continue
        rows = []
        with open(csv_path) as f:
            for r in csv.DictReader(f):
                if r.get("source_agency") != agency or r.get("source_list") != list_name:
                    continue
                rows.append(tuple(source_id if c == "source_id" else r.get(c, "") for c in SCHEMA))
        if not rows:
            print(f"  [{label}] {source_id}: 0 rows in CSV")
            continue
        cur.execute("DELETE FROM watchlist_records WHERE source_id = %s;", (source_id,))
        n_del = cur.rowcount
        psycopg2.extras.execute_values(
            cur,
            f"INSERT INTO watchlist_records ({','.join(SCHEMA)}) VALUES %s",
            rows, page_size=500,
        )
        n_ins = cur.rowcount
        total_inserted += n_ins
        print(f"  [{label}] {source_id:40s}  del={n_del:>5d}  ins={n_ins:>5d}")
    conn.commit()
    cur.execute("SELECT COUNT(*) FROM watchlist_records;")
    n_total = cur.fetchone()[0]
    print(f"  [{label}] total inserted this run: {total_inserted}, db_total={n_total:,}")
    conn.close()


if __name__ == "__main__":
    load_into("local", LOCAL)
    load_into("RDS  ", RDS)
