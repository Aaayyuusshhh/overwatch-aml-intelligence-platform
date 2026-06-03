#!/usr/bin/env python3
"""Phase-1 fetcher: pull direct CSVs / JSON APIs that don't need a scraper.

Strategy: every source_id below has a publicly-downloadable feed in a known
shape. We HTTP GET, transform to the 17-col canonical schema, write
data/<source_id>.csv, register in sources.json, then return — the caller
loads to local DB.

The script is deliberately fail-soft per-source: any single fetch that 4xx's
or throws skips that source and continues. The whole point is to take wins
where they're easy and not block on flaky upstream.
"""
from __future__ import annotations
import csv
import io
import json
import os
import re
import sys
import time
from datetime import datetime, timezone

import requests
import urllib3
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.path.join(PROJECT_ROOT, "data")
SOURCES_JSON = os.path.join(PROJECT_ROOT, "sources.json")
os.makedirs(DATA_DIR, exist_ok=True)

CANONICAL_COLS = [
    "source_agency", "source_list", "case_unit", "name", "father_name",
    "date_of_birth", "gender", "address", "reward_amount", "details",
    "has_document", "document_url", "detail_page_url", "interpol_notice_id",
    "link_kind", "scraped_at", "enrichment_status",
]

UA = {
    "User-Agent": ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"),
    "Accept": "text/csv,application/json,*/*",
}


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _row(**kw):
    base = {c: "" for c in CANONICAL_COLS}
    base["scraped_at"] = _now_iso()
    for k, v in kw.items():
        if k in CANONICAL_COLS and v is not None:
            base[k] = str(v)[:1000] if k == "details" else str(v)[:300]
    return base


def _write_csv(rows: list[dict], out_path: str) -> int:
    with open(out_path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=CANONICAL_COLS, extrasaction="ignore")
        w.writeheader()
        for r in rows:
            w.writerow({k: r.get(k, "") for k in CANONICAL_COLS})
    return len(rows)


def _fresh_get(url, **kw):
    """Per-call Session — avoids urllib3 pool poisoning across slow sources
    (the same root cause caught last week in scrape_blocked_sources.py)."""
    s = requests.Session()
    s.headers.update({"Connection": "close"})
    try:
        return s.get(url, **kw)
    finally:
        s.close()


# ────────────────────────────────────────────────────────────────────────────
# Source-specific fetchers
# ────────────────────────────────────────────────────────────────────────────

def fetch_opensanctions(name: str, agency: str, list_name: str,
                        target_count_hint: int = 0) -> list[dict]:
    """One row per entity in the dataset's targets.simple.csv. Per-entity
    proof link is the canonical opensanctions.org page for that entity."""
    url = (f"https://data.opensanctions.org/datasets/latest/"
           f"{name}/targets.simple.csv")
    r = _fresh_get(url, headers={**UA, "Accept": "text/csv"},
                   timeout=60, verify=False, allow_redirects=True)
    if r.status_code != 200:
        raise RuntimeError(f"HTTP {r.status_code}")
    head = r.text[:120].lstrip("﻿").lstrip()
    if not head.startswith(("id,", '"id"')):
        raise RuntimeError(f"unexpected payload (first 80 chars: {head[:80]!r})")
    rows = []
    rdr = csv.DictReader(io.StringIO(r.text))
    for src in rdr:
        ent_id = (src.get("id") or "").strip()
        nm = (src.get("name") or "").strip()
        if not nm or len(nm) < 2:
            continue
        aliases = (src.get("aliases") or "").strip()
        countries = (src.get("countries") or "").strip()
        addresses = (src.get("addresses") or "").strip()
        idents = (src.get("identifiers") or "").strip()
        sancs = (src.get("sanctions") or "").strip()
        birth = (src.get("birth_date") or "").strip()
        schema = (src.get("schema") or "").strip()
        ds_name = (src.get("dataset") or name).strip()
        bits = []
        if schema:
            bits.append(f"Schema: {schema}")
        if countries:
            bits.append(f"Countries: {countries[:120]}")
        if aliases:
            bits.append(f"Aliases: {aliases[:200]}")
        if idents:
            bits.append(f"IDs: {idents[:200]}")
        if sancs:
            bits.append(f"Sanctions: {sancs[:200]}")
        bits.append(f"Dataset: {ds_name}")
        rows.append(_row(
            source_agency=agency,
            source_list=list_name,
            case_unit=ent_id,
            name=nm,
            date_of_birth=birth[:30],
            address=addresses[:300],
            details=" | ".join(bits),
            has_document="Yes",
            detail_page_url=f"https://www.opensanctions.org/entities/{ent_id}/" if ent_id else url,
            link_kind="opensanctions_entity",
        ))
    return rows


# ────────────────────────────────────────────────────────────────────────────
# Source registry (id, fetcher, args, registration metadata)
# ────────────────────────────────────────────────────────────────────────────

OS_FEEDS = [
    # (os_dataset_name, our source_id, agency, list_name)
    ("us_hhs_exclusions",          "us_hhs_oig_exclusions",
     "US Health and Human Services Office of Inspector General",
     "OIG Exclusions List"),
    ("sg_gov_dir",                 "sg_government_directory",
     "Government of Singapore",
     "Government Directory (Officials)"),
    ("us_finra_actions",           "us_finra_enforcement_actions",
     "US Financial Industry Regulatory Authority (FINRA)",
     "Enforcement Actions"),
    ("ca_dfatd_sema_sanctions",    "ca_sema_consolidated_sanctions",
     "Global Affairs Canada (GAC)",
     "Consolidated Autonomous Sanctions List (SEMA)"),
    ("eg_terrorists",              "egypt_domestic_terrorist_list",
     "Government of Egypt",
     "Domestic Terrorist List"),
    ("au_dfat_sanctions",          "au_dfat_consolidated_sanctions",
     "Australian Department of Foreign Affairs and Trade (DFAT)",
     "Consolidated Sanctions List"),
    ("fr_hatvp_declarations",      "fr_hatvp_interest_declarations",
     "Haute Autorité pour la transparence de la vie publique (HATVP) France",
     "Declarations of Interests and Assessments"),
    ("pt_parliament",              "pt_assembleia_republica_members",
     "Assembleia da República Portugal",
     "Members of the Assembly (PEPs)"),
    ("us_cftc_enforcement_actions","us_cftc_enforcement_actions",
     "US Commodity Futures Trading Commission (CFTC)",
     "Enforcement Actions"),
    ("us_navy",                    "us_navy_leadership",
     "US Department of the Navy",
     "Leadership / Senior Officials (PEPs)"),
    ("us_state_dept",              "us_state_dept_senior_officials",
     "US Department of State",
     "Senior Officials (PEPs)"),
    ("si_dz_rs",                   "si_national_assembly_members",
     "Slovenia National Assembly",
     "Members of the National Assembly (PEPs)"),
    ("be_walloon_parliament",      "be_walloon_parliament_members",
     "Parlement de Wallonie",
     "Parliament of Wallonia Members (PEPs)"),
    ("sg_terrorists",              "sg_targeted_financial_sanctions_terrorists",
     "Government of Singapore",
     "Targeted Financial Sanctions (Terrorism)"),
    ("us_fincen_special_measures", "us_fincen_311_9714_special_measures",
     "US Treasury Financial Crimes Enforcement Network (FinCEN)",
     "Special Measures (311 / 9714)"),
    ("eg_terrorists",              "eg_terrorism_list_subordinate",
     "Government of Egypt",
     "Terrorism List (extended)"),  # near-duplicate kept for distinct registration
]

# Skip the bottom one — it's the same dataset as the first eg_terrorists.
OS_FEEDS = OS_FEEDS[:15]


def register_in_sources_json(entries: list[dict]) -> int:
    with open(SOURCES_JSON) as f:
        data = json.load(f)
    have = {s["id"] for s in data["sources"]}
    added = 0
    for e in entries:
        if e["id"] in have:
            continue
        data["sources"].append(e)
        have.add(e["id"])
        added += 1
    with open(SOURCES_JSON, "w") as f:
        json.dump(data, f, indent=2)
    return added


def _source_entry(sid, agency, list_name, url, country="International",
                  notes=""):
    return {
        "id": sid,
        "agency": agency,
        "list_name": list_name,
        "url": url,
        "type": "html",
        "scraper": None,
        "expected_min_records": 0,
        "status": "active",
        "change_detection": False,
        "change_detection_selector": None,
        "country": country,
        "notes": notes,
    }


def main():
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", help="run only this source_id")
    args = ap.parse_args()

    results = []
    registrations = []
    for ds_name, sid, agency, lst in OS_FEEDS:
        if args.only and sid != args.only:
            continue
        print(f"\n[{sid}]", flush=True)
        t0 = time.time()
        try:
            rows = fetch_opensanctions(ds_name, agency, lst)
        except Exception as e:
            print(f"  FAIL: {type(e).__name__}: {str(e)[:140]}", flush=True)
            results.append({"id": sid, "status": "fail",
                             "error": f"{type(e).__name__}: {str(e)[:160]}",
                             "rows": 0, "elapsed_s": round(time.time()-t0, 1)})
            continue
        if not rows:
            print(f"  EMPTY: 0 rows", flush=True)
            results.append({"id": sid, "status": "empty", "rows": 0,
                             "elapsed_s": round(time.time()-t0, 1)})
            continue
        out_path = os.path.join(DATA_DIR, f"{sid}.csv")
        n = _write_csv(rows, out_path)
        elapsed = time.time() - t0
        print(f"  OK: {n:,} rows -> {out_path} ({elapsed:.1f}s)", flush=True)
        results.append({"id": sid, "status": "success", "rows": n,
                         "csv": out_path,
                         "elapsed_s": round(elapsed, 1)})
        registrations.append(_source_entry(
            sid, agency, lst,
            f"https://www.opensanctions.org/datasets/{ds_name}/",
            country="International",
            notes=f"OpenSanctions feed {ds_name}; auto-loaded via "
                  "scripts/fetch_easy_sources.py."))

    if registrations:
        added = register_in_sources_json(registrations)
        print(f"\nRegistered {added} new sources in sources.json", flush=True)

    n_ok = sum(1 for r in results if r["status"] == "success")
    total_rows = sum(r["rows"] for r in results)
    print(f"\n=== Phase-1 fetch summary ===")
    print(f"  success: {n_ok}/{len(results)}")
    print(f"  total rows: {total_rows:,}")

    # Persist summary for the loader
    summary_path = os.path.join(os.path.dirname(__file__),
                                 "fetch_easy_sources.summary.json")
    with open(summary_path, "w") as f:
        json.dump({"started_at": _now_iso(), "results": results}, f, indent=2)


if __name__ == "__main__":
    sys.exit(main())
