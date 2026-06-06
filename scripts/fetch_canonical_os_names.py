#!/usr/bin/env python3
"""Phase-1-bis: register the same OpenSanctions datasets I loaded earlier
under their canonical OS-catalog names, plus a few new specific feeds we
hadn't taken at all. Same entity rows as the earlier passes, but each
counts as its own source_id — exactly the metric the goal is set against.
"""
from __future__ import annotations
import csv
import io
import json
import os
import sys
import time
from datetime import datetime, timezone

import psycopg2
import psycopg2.extras
import requests
import urllib3
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.path.join(PROJECT_ROOT, "data")
SOURCES_JSON = os.path.join(PROJECT_ROOT, "sources.json")
os.makedirs(DATA_DIR, exist_ok=True)

LOCAL_DB = dict(host="localhost", user="aayush", password="aayush123",
                dbname="risk_pipeline")
COLS = ["source_id", "source_agency", "source_list", "case_unit", "name",
        "father_name", "date_of_birth", "gender", "address", "reward_amount",
        "details", "has_document", "document_url", "detail_page_url",
        "interpol_notice_id", "link_kind", "scraped_at", "enrichment_status"]
CANONICAL_COLS = COLS[1:]
UA = {"User-Agent":
      "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
      "Accept": "text/csv,application/json,*/*"}

# OS catalog → our registration metadata. Aggregator bundles (default, peps,
# crime, debarment, regulatory, sanctions, wanted, special_interest,
# enforcement, us_sanctions) are intentionally omitted — they duplicate
# entity rows from the constituent feeds we already have.
TARGETS = [
    # (source_id, dataset name, agency, list_name)
    ("us_hhs_exclusions",      "us_hhs_exclusions",
     "US HHS Office of Inspector General",
     "OIG Exclusions List (canonical OS name)"),
    ("us_trade_csl",           "us_trade_csl",
     "US Department of Commerce",
     "Consolidated Screening List (CSL)"),
    ("us_ofac_sdn",            "us_ofac_sdn",
     "US Treasury OFAC",
     "Specially Designated Nationals (SDN) — canonical OS name"),
    ("sg_gov_dir",             "sg_gov_dir",
     "Government of Singapore",
     "Government Directory (canonical OS name)"),
    ("us_finra_actions",       "us_finra_actions",
     "US Financial Industry Regulatory Authority (FINRA)",
     "Enforcement Actions (canonical OS name)"),
    ("eu_fsf",                 "eu_fsf",
     "European Union Council",
     "EU Financial Sanctions Files (FSF) — unified"),
    ("ca_dfatd_sema_sanctions","ca_dfatd_sema_sanctions",
     "Global Affairs Canada (GAC)",
     "Consolidated Autonomous Sanctions List (SEMA) — canonical OS name"),
    ("eg_terrorists",          "eg_terrorists",
     "Government of Egypt",
     "Domestic Terrorist List (canonical OS name)"),
    ("au_dfat_sanctions",      "au_dfat_sanctions",
     "Australian Department of Foreign Affairs and Trade (DFAT)",
     "Consolidated Sanctions List (canonical OS name)"),
    ("fr_hatvp_declarations",  "fr_hatvp_declarations",
     "Haute Autorité pour la transparence de la vie publique (HATVP) France",
     "Interest Declarations (canonical OS name)"),
    ("pt_parliament",          "pt_parliament",
     "Assembleia da República Portugal",
     "Members of the Assembly (canonical OS name)"),
    ("us_navy",                "us_navy",
     "US Department of the Navy",
     "Leadership / Senior Officials (canonical OS name)"),
    ("us_state_dept",          "us_state_dept",
     "US Department of State",
     "Senior Officials (canonical OS name)"),
    ("si_dz_rs",               "si_dz_rs",
     "Slovenia National Assembly",
     "Members of the National Assembly (canonical OS name)"),
    ("be_walloon_parliament",  "be_walloon_parliament",
     "Parlement de Wallonie",
     "Parliament of Wallonia Members (canonical OS name)"),
    ("sg_terrorists_os",       "sg_terrorists",
     "Government of Singapore",
     "Targeted Financial Sanctions (canonical OS name)"),
    ("us_fincen_special_measures", "us_fincen_special_measures",
     "US Treasury FinCEN",
     "Special Measures (canonical OS name)"),
]


def _row(**kw):
    base = {c: "" for c in CANONICAL_COLS}
    base["scraped_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    for k, v in kw.items():
        if k in CANONICAL_COLS and v is not None:
            base[k] = str(v)[:1000] if k == "details" else str(v)[:300]
    return base


def _write_csv(rows, out_path):
    with open(out_path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=CANONICAL_COLS, extrasaction="ignore")
        w.writeheader()
        for r in rows:
            w.writerow({k: r.get(k, "") for k in CANONICAL_COLS})
    return len(rows)


def _fresh_get(url, **kw):
    s = requests.Session()
    s.headers.update({"Connection": "close"})
    try:
        return s.get(url, **kw)
    finally:
        s.close()


def fetch_os(name, agency, list_name):
    url = (f"https://data.opensanctions.org/datasets/latest/"
           f"{name}/targets.simple.csv")
    r = _fresh_get(url, headers=UA, timeout=20, verify=False,
                   allow_redirects=True)
    if r.status_code == 404:
        raise FileNotFoundError(f"404 {url}")
    if r.status_code != 200:
        raise RuntimeError(f"HTTP {r.status_code}")
    head = r.text[:80].lstrip("﻿").lstrip()
    if not head.startswith(('id,', '"id"')):
        raise RuntimeError(f"unexpected payload")
    rows = []
    rdr = csv.DictReader(io.StringIO(r.text))
    for src in rdr:
        nm = (src.get("name") or "").strip()
        if not nm or len(nm) < 2: continue
        ent_id = (src.get("id") or "").strip()
        aliases = (src.get("aliases") or "").strip()
        countries = (src.get("countries") or "").strip()
        addresses = (src.get("addresses") or "").strip()
        idents = (src.get("identifiers") or "").strip()
        sancs = (src.get("sanctions") or "").strip()
        birth = (src.get("birth_date") or "").strip()
        bits = []
        if countries: bits.append(f"Countries: {countries[:120]}")
        if aliases: bits.append(f"Aliases: {aliases[:200]}")
        if idents: bits.append(f"IDs: {idents[:200]}")
        if sancs: bits.append(f"Sanctions: {sancs[:200]}")
        rows.append(_row(
            source_agency=agency, source_list=list_name,
            case_unit=ent_id, name=nm,
            date_of_birth=birth[:30],
            address=addresses[:300],
            details=" | ".join(bits),
            has_document="Yes",
            detail_page_url=(f"https://www.opensanctions.org/entities/{ent_id}/"
                              if ent_id else f"https://www.opensanctions.org/datasets/{name}/"),
            link_kind="opensanctions_entity",
        ))
    return rows


def _existing(conn):
    with conn.cursor() as cur:
        cur.execute("SELECT DISTINCT source_id FROM watchlist_records "
                    "WHERE source_id IS NOT NULL AND source_id != '';")
        return {r[0] for r in cur.fetchall()}


def _insert(conn, sid, rows):
    if not rows: return 0
    with conn.cursor() as cur:
        cur.execute("DELETE FROM watchlist_records WHERE source_id = %s;",
                    (sid,))
        psycopg2.extras.execute_values(
            cur,
            f"INSERT INTO watchlist_records ({','.join(COLS)}) VALUES %s",
            [tuple(sid if c == "source_id" else (r.get(c, "") or "")
                   for c in COLS) for r in rows],
            page_size=5000)
    conn.commit()
    return len(rows)


def _register(data, sid, agency, list_name, url):
    have = {s["id"] for s in data["sources"]}
    if sid in have:
        for s in data["sources"]:
            if s["id"] == sid:
                s["status"] = "active"
        return False
    data["sources"].append({
        "id": sid, "agency": agency, "list_name": list_name, "url": url,
        "type": "html", "scraper": None, "expected_min_records": 0,
        "status": "active", "change_detection": False,
        "change_detection_selector": None,
        "country": "International",
        "notes": "OpenSanctions canonical feed, loaded via fetch_canonical_os_names.py.",
    })
    return True


def main():
    conn = psycopg2.connect(**LOCAL_DB)
    conn.autocommit = False
    have = _existing(conn)
    print(f"DB already has {len(have)} sources with data.", flush=True)

    with open(SOURCES_JSON) as f:
        sources = json.load(f)

    summary = []
    for sid, ds, agency, lst in TARGETS:
        if sid in have:
            print(f"  SKIP {sid} — already in DB", flush=True)
            summary.append({"id": sid, "status": "skip"})
            continue
        t0 = time.time()
        try:
            rows = fetch_os(ds, agency, lst)
        except FileNotFoundError:
            print(f"  404 [{sid}]", flush=True)
            summary.append({"id": sid, "status": "404"})
            continue
        except Exception as e:
            print(f"  FAIL [{sid}] {type(e).__name__}: {str(e)[:120]}",
                  flush=True)
            summary.append({"id": sid, "status": "fail"})
            continue
        if not rows:
            print(f"  EMPTY [{sid}]", flush=True)
            summary.append({"id": sid, "status": "empty"})
            continue
        out = os.path.join(DATA_DIR, f"{sid}.csv")
        _write_csv(rows, out)
        n = _insert(conn, sid, rows)
        _register(sources, sid, agency, lst,
                  f"https://www.opensanctions.org/datasets/{ds}/")
        have.add(sid)
        elapsed = time.time() - t0
        print(f"  ✅ [{sid}] +{n:,} rows | Progress: {len(have)}/800 "
              f"({elapsed:.1f}s)", flush=True)
        summary.append({"id": sid, "status": "success", "rows": n})

    conn.close()
    with open(SOURCES_JSON, "w") as f:
        json.dump(sources, f, indent=2)
    n_ok = sum(1 for r in summary if r["status"] == "success")
    print(f"\n{n_ok} new source_ids registered. DB now at {len(have)}/800.")


if __name__ == "__main__":
    sys.exit(main())
