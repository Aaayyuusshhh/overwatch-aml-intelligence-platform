#!/usr/bin/env python3
"""Load all extended MCA CSVs into local DB + RDS via targeted insert."""
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

# (source_id, source_agency, source_list, csv_path)
LOADS = [
    ("mca_defaulter_companies",
     "Ministry of Corporate Affairs (MCA)",
     "Defaulter Companies (Filing Default)",
     "data/mca_defaulter_companies.csv"),
    ("mca_defaulter_directors",
     "Ministry of Corporate Affairs (MCA)",
     "Defaulter Directors (Filing Default)",
     "data/mca_defaulter_directors.csv"),
    ("mca_dormant_companies",
     "Ministry of Corporate Affairs (MCA)",
     "Dormant Companies (3yr Filing Default)",
     "data/mca_dormant_companies.csv"),
    ("mca_llps_strike_off",
     "Ministry of Corporate Affairs (MCA)",
     "LLPs Under Process of Strike Off",
     "data/mca_llps_strike_off.csv"),
    ("mca_public_notices_stk6",
     "Ministry of Corporate Affairs (MCA)",
     "Public Notices (STK-6) U/S 248(2)",
     "data/mca_public_notices_stk6.csv"),
    ("mca_corporate_fraud_chit_fund",
     "Ministry of Corporate Affairs (MCA)",
     "Companies Involved in Corporate Frauds / Chit Fund Scams",
     "data/mca_corporate_fraud_chit_fund.csv"),
    ("mca_vanishing_companies",
     "Ministry of Corporate Affairs (MCA)",
     "Vanishing Companies (MCA via WatchOut)",
     "data/mca_vanishing_companies.csv"),
]


def load_into(label, conn_kwargs):
    conn = psycopg2.connect(**conn_kwargs)
    conn.autocommit = False
    cur = conn.cursor()
    for source_id, agency, list_name, csv_rel in LOADS:
        csv_path = os.path.join(PROJECT, csv_rel)
        if not os.path.exists(csv_path):
            print(f"  [{label}] {source_id}: CSV missing — skip")
            continue
        rows = []
        with open(csv_path, encoding="utf-8") as f:
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
        cur.execute("SELECT COUNT(*) FROM watchlist_records WHERE source_id = %s;",
                    (source_id,))
        n_after = cur.fetchone()[0]
        print(f"  [{label}] {source_id:40s}  del={n_del:>5d}  loaded={n_after:>5d}")
    conn.commit()
    cur.execute("SELECT COUNT(*) FROM watchlist_records;")
    print(f"  [{label}] db_total = {cur.fetchone()[0]:,}")
    conn.close()


if __name__ == "__main__":
    load_into("local", LOCAL)
    load_into("RDS  ", RDS)
