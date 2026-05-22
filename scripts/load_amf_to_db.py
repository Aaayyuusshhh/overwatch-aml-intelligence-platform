#!/usr/bin/env python3
"""Load AMF France blacklists CSV into local DB + RDS via targeted insert."""
import csv, json, os, sys
import psycopg2
import psycopg2.extras

PROJECT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CSV = os.path.join(PROJECT, "data/amf_france_blacklists.csv")

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

SOURCE_ID = "amf_france_blacklists"
AGENCY = "Autorité des marchés financiers (AMF)"
LIST_NAME = "Blacklists of unauthorised companies"


def load_into(label, conn_kwargs):
    rows = []
    with open(CSV) as f:
        for r in csv.DictReader(f):
            if r["source_agency"] != AGENCY or r["source_list"] != LIST_NAME:
                continue
            rows.append(tuple(SOURCE_ID if c == "source_id" else r.get(c, "") for c in SCHEMA))
    if not rows:
        print(f"[{label}] no rows in CSV")
        return
    conn = psycopg2.connect(**conn_kwargs)
    conn.autocommit = False
    cur = conn.cursor()
    cur.execute("DELETE FROM watchlist_records WHERE source_id = %s;", (SOURCE_ID,))
    n_del = cur.rowcount
    psycopg2.extras.execute_values(
        cur,
        f"INSERT INTO watchlist_records ({','.join(SCHEMA)}) VALUES %s",
        rows, page_size=200,
    )
    n_ins = cur.rowcount
    conn.commit()
    cur.execute("SELECT COUNT(*) FROM watchlist_records WHERE source_id = %s;", (SOURCE_ID,))
    n_after = cur.fetchone()[0]
    cur.execute("SELECT COUNT(*) FROM watchlist_records;")
    n_total = cur.fetchone()[0]
    print(f"[{label}] deleted={n_del}, inserted={n_ins}, source_now={n_after}, db_total={n_total:,}")
    conn.close()


if __name__ == "__main__":
    load_into("local", LOCAL)
    load_into("RDS  ", RDS)
