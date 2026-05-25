#!/usr/bin/env python3
"""Load the extended MCA CSVs into local DB + RDS via targeted insert.

Covers:
  - mca_defaulter_companies         (CSV from scrapers/mca_company_llp.py)
  - mca_defaulter_directors         (CSV from scrapers/mca_company_llp.py)
  - mca_dormant_companies           (CSV from scrapers/mca_company_llp.py)
  - mca_corporate_fraud_chit_fund   (CSV from scrapers/mca_corporate_fraud_chit_fund.py)

For each: DELETE rows with this source_id, then INSERT fresh rows from CSV.
No global DELETE.
"""
import csv, os
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
    ("mca_defaulter_companies",
     "Ministry of Corporate Affairs (MCA)",
     "Defaulter Companies (Filing Default)",
     os.path.join(PROJECT, "data/mca_defaulter_companies.csv")),
    ("mca_defaulter_directors",
     "Ministry of Corporate Affairs (MCA)",
     "Defaulter Directors (Filing Default)",
     os.path.join(PROJECT, "data/mca_defaulter_directors.csv")),
    ("mca_dormant_companies",
     "Ministry of Corporate Affairs (MCA)",
     "Dormant Companies (3yr Filing Default)",
     os.path.join(PROJECT, "data/mca_dormant_companies.csv")),
    ("mca_corporate_fraud_chit_fund",
     "Ministry of Corporate Affairs (MCA)",
     "Companies Involved in Corporate Frauds / Chit Fund Scams",
     os.path.join(PROJECT, "data/mca_corporate_fraud_chit_fund.csv")),
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
        print(f"  [{label}] {source_id:40s}  del={n_del:>6d}  ins={n_ins:>6d}")
    conn.commit()
    cur.execute("SELECT COUNT(*) FROM watchlist_records;")
    n_total = cur.fetchone()[0]
    print(f"  [{label}] total inserted this run: {total_inserted}, db_total={n_total:,}")
    conn.close()


if __name__ == "__main__":
    load_into("local", LOCAL)
    load_into("RDS  ", RDS)
