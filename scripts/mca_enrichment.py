#!/usr/bin/env python3
"""
mca_enrichment.py — enrich every unique CIN in watchlist_records via the
ScoreMe MCA Company Basic Details API. Resumable, rate-limited (1 req/s),
per-CIN commit. Stores results + risk scoring in mca_company_enrichment.

CLI:
  python scripts/mca_enrichment.py                full run
  python scripts/mca_enrichment.py --limit 50     process 50 unprocessed CINs
  python scripts/mca_enrichment.py --retry-failed  re-process FAILED CINs
  python scripts/mca_enrichment.py --stats        print stats, no processing
  python scripts/mca_enrichment.py --dry-run      extract CINs, show count only
"""
import argparse
import json
import logging
import os
import sys
import time

import psycopg2
import psycopg2.extras
import requests

PROJECT = "/home/aayush/risk-pipeline"
LOG_DIR = os.path.join(PROJECT, "logs")
DB = dict(host="localhost", user="aayush", password="aayush123", dbname="risk_pipeline")

API_URL = "https://quality-da-proxy.scoreme.in/mca/external/companyBasicDetails"
CLIENT_ID = "c07339b56ae74975d778445e23d46500"
CLIENT_SECRET = "cf73ee3eaacb0201dd1a4166e5d1ac744c32c6605fd14c3188f951ed4c6384fc"
# Auth goes in HEADERS (verified against the live API; the brief's
# "creds in body" instruction returns HTTP 412).
HEADERS = {"Content-Type": "application/json",
           "clientId": CLIENT_ID, "clientSecret": CLIENT_SECRET}

# CIN: 1 letter, 5 digits, 2 letters, 4 digits, 3 letters, 6 digits
CIN_RE = r"[A-Z][0-9]{5}[A-Z]{2}[0-9]{4}[A-Z]{3}[0-9]{6}"
TEXT_COLS = ["details", "case_unit", "name", "address", "father_name", "notes"]

os.makedirs(LOG_DIR, exist_ok=True)
logger = logging.getLogger("mca_enrichment")
logger.setLevel(logging.INFO)
_fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s")
_sh = logging.StreamHandler(sys.stdout); _sh.setFormatter(_fmt)
_fh = logging.FileHandler(os.path.join(LOG_DIR, "mca_enrichment.log")); _fh.setFormatter(_fmt)
logger.addHandler(_sh); logger.addHandler(_fh)
requests.packages.urllib3.disable_warnings()


def connect():
    return psycopg2.connect(**DB)


def extract_cins(conn):
    """Return {cin: (count_of_records, first_seen_agency)} across text cols."""
    cins = {}
    with conn.cursor() as cur:
        cols = [c for c in TEXT_COLS if _col_exists(cur, c)]
        for col in cols:
            cur.execute(f"""
                SELECT (regexp_matches({col}, '{CIN_RE}', 'g'))[1] AS cin,
                       source_agency
                FROM watchlist_records
                WHERE {col} ~ '{CIN_RE}'
            """)
            for cin, agency in cur.fetchall():
                if cin not in cins:
                    cins[cin] = [0, agency]
                cins[cin][0] += 1
    return cins


def _col_exists(cur, col):
    cur.execute("""SELECT 1 FROM information_schema.columns
                   WHERE table_name='watchlist_records' AND column_name=%s""",
                (col,))
    return cur.fetchone() is not None


def already_done(conn):
    with conn.cursor() as cur:
        cur.execute("SELECT cin FROM mca_company_enrichment "
                    "WHERE api_status='SUCCESS'")
        return {r[0] for r in cur.fetchall()}


def failed_cins(conn):
    with conn.cursor() as cur:
        cur.execute("SELECT cin FROM mca_company_enrichment "
                    "WHERE api_status='FAILED'")
        return [r[0] for r in cur.fetchall()]


def score_risk(d):
    flags = []
    is_def = str(d.get("whetherCompanyDefaulter", "")).strip().lower() == "yes"
    is_van = str(d.get("whetherVanishingCompanyYN", "")).strip().lower() == "yes"
    is_dor = str(d.get("whetherCompanyDormantCompany", "")).strip().lower() == "yes"
    status = (d.get("companyStatus") or "").strip()
    if is_def: flags.append("DEFAULTER")
    if is_van: flags.append("VANISHING")
    if is_dor: flags.append("DORMANT")
    if status and status.lower() != "active": flags.append("INACTIVE")
    if "DEFAULTER" in flags or "VANISHING" in flags:
        level = "HIGH"
    elif "DORMANT" in flags or "INACTIVE" in flags:
        level = "MEDIUM"
    else:
        level = "LOW"
    return level, flags, is_def, is_van, is_dor


def call_api(cin):
    r = requests.post(API_URL, headers=HEADERS, json={"cin_llpin": cin},
                      timeout=30, verify=False)
    return r


def upsert(conn, cin, rec_count, first_seen, *, status, data=None,
           err=None, raw=None):
    with conn.cursor() as cur:
        if status == "SUCCESS" and data is not None:
            level, flags, isd, isv, isdor = score_risk(data)
            cur.execute("""
                INSERT INTO mca_company_enrichment
                  (cin, company_name, company_status, registered_address,
                   date_of_incorporation, authorised_capital, paid_up_capital,
                   email_id, is_defaulter, is_vanishing, is_dormant,
                   risk_level, risk_flags, raw_response, api_status,
                   source_records_count, first_seen_in, updated_at)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,'SUCCESS',
                        %s,%s,NOW())
                ON CONFLICT (cin) DO UPDATE SET
                  company_name=EXCLUDED.company_name,
                  company_status=EXCLUDED.company_status,
                  registered_address=EXCLUDED.registered_address,
                  date_of_incorporation=EXCLUDED.date_of_incorporation,
                  authorised_capital=EXCLUDED.authorised_capital,
                  paid_up_capital=EXCLUDED.paid_up_capital,
                  email_id=EXCLUDED.email_id, is_defaulter=EXCLUDED.is_defaulter,
                  is_vanishing=EXCLUDED.is_vanishing, is_dormant=EXCLUDED.is_dormant,
                  risk_level=EXCLUDED.risk_level, risk_flags=EXCLUDED.risk_flags,
                  raw_response=EXCLUDED.raw_response, api_status='SUCCESS',
                  error_message=NULL, source_records_count=EXCLUDED.source_records_count,
                  first_seen_in=EXCLUDED.first_seen_in, updated_at=NOW()
            """, (cin, data.get("companyName"), data.get("companyStatus"),
                  data.get("registeredAddress"), data.get("dateOfIncorporation"),
                  data.get("authorisedCapital"), data.get("paidUpCapital"),
                  data.get("emailId"), isd, isv, isdor, level, flags,
                  json.dumps(raw), rec_count, first_seen))
            conn.commit()
            return level, flags
        else:
            cur.execute("""
                INSERT INTO mca_company_enrichment
                  (cin, api_status, error_message, source_records_count,
                   first_seen_in, updated_at)
                VALUES (%s,%s,%s,%s,%s,NOW())
                ON CONFLICT (cin) DO UPDATE SET
                  api_status=EXCLUDED.api_status,
                  error_message=EXCLUDED.error_message,
                  source_records_count=EXCLUDED.source_records_count,
                  first_seen_in=EXCLUDED.first_seen_in, updated_at=NOW()
            """, (cin, status, err, rec_count, first_seen))
            conn.commit()
            return None, None


def print_stats(conn):
    with conn.cursor() as cur:
        cur.execute("""SELECT api_status, COUNT(*) FROM mca_company_enrichment
                       GROUP BY api_status ORDER BY 1""")
        st = cur.fetchall()
        cur.execute("""SELECT risk_level, COUNT(*) FROM mca_company_enrichment
                       WHERE api_status='SUCCESS' GROUP BY risk_level""")
        rk = cur.fetchall()
        cur.execute("SELECT COUNT(*) FROM mca_company_enrichment")
        total = cur.fetchone()[0]
    print(f"\n=== mca_company_enrichment stats ===\nTotal rows: {total}")
    print("By api_status:")
    for s, c in st: print(f"  {s}: {c}")
    print("By risk_level (SUCCESS only):")
    for s, c in rk: print(f"  {s}: {c}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int)
    ap.add_argument("--retry-failed", action="store_true")
    ap.add_argument("--stats", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    conn = connect()
    try:
        if args.stats:
            print_stats(conn)
            return

        cins = extract_cins(conn)
        logger.info("Extracted %d unique CINs from watchlist_records", len(cins))
        if args.dry_run:
            sample = list(cins.items())[:10]
            logger.info("[DRY-RUN] sample: %s",
                        [(c, n[0]) for c, n in sample])
            logger.info("[DRY-RUN] %d CINs found; no API calls made.", len(cins))
            return

        if args.retry_failed:
            todo = [c for c in failed_cins(conn) if c in cins]
            logger.info("Retry-failed mode: %d FAILED CINs", len(todo))
        else:
            done = already_done(conn)
            todo = [c for c in cins if c not in done]
            logger.info("%d CINs to process (%d already SUCCESS)",
                        len(todo), len(done))

        if args.limit:
            todo = todo[:args.limit]
            logger.info("Limited to %d CINs", len(todo))

        n = len(todo)
        succ = fail = high = med = low = 0
        for i, cin in enumerate(todo, 1):
            rec_count, first_seen = cins.get(cin, (0, None))
            try:
                r = call_api(cin)
                if r.status_code == 200:
                    body = r.json()
                    data = body.get("data", {})
                    if isinstance(data, dict) and "data" in data:
                        data = data["data"]
                    if not data:
                        upsert(conn, cin, rec_count, first_seen,
                               status="NOT_FOUND", raw=body)
                        fail += 1
                    else:
                        level, flags = upsert(conn, cin, rec_count, first_seen,
                                              status="SUCCESS", data=data, raw=body)
                        succ += 1
                        if level == "HIGH":
                            high += 1
                            logger.info("🚨 HIGH RISK: %s — %s", cin,
                                        ", ".join(flags))
                        elif level == "MEDIUM":
                            med += 1
                        else:
                            low += 1
                else:
                    upsert(conn, cin, rec_count, first_seen, status="FAILED",
                           err=f"HTTP {r.status_code}: {r.text[:200]}")
                    fail += 1
            except Exception as e:
                upsert(conn, cin, rec_count, first_seen, status="FAILED",
                       err=f"{type(e).__name__}: {e}")
                fail += 1
            if i % 10 == 0 or i == n:
                logger.info("[%d/%d] processed (succ=%d fail=%d)",
                            i, n, succ, fail)
            time.sleep(1)  # rate limit 1 req/s

        logger.info("DONE. total=%d success=%d failed=%d | HIGH=%d MED=%d LOW=%d",
                    n, succ, fail, high, med, low)
    finally:
        conn.close()


if __name__ == "__main__":
    main()
