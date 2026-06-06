#!/usr/bin/env python3
"""Bulk-load the remaining direct CSVs and OpenSanctions per-country feeds
to push the with-data source count past 800.

Strategy:
  - 4 direct CSV downloads (UK OFSI, OFAC SDN, OFAC consolidated, BIS).
  - 42 OpenSanctions per-jurisdiction targets.simple.csv feeds.
  - Skip any source_id that already has rows in the DB (assumes
    "with-data" is the metric we want to grow).
  - 10-second download timeout. No retries. Fail soft per-source.
  - DELETE+INSERT into local DB after each successful fetch.
  - Register every new source in sources.json with status=active.

Output:
  - data/<source_id>.csv per success.
  - scripts/fetch_remaining_sources.summary.json (per-source result).
  - One progress line per success printed to stdout.
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
SUMMARY_PATH = os.path.join(os.path.dirname(__file__),
                            "fetch_remaining_sources.summary.json")
os.makedirs(DATA_DIR, exist_ok=True)

LOCAL_DB = dict(host="localhost", user="aayush", password="aayush123",
                dbname="risk_pipeline")

COLS = ["source_id", "source_agency", "source_list", "case_unit", "name",
        "father_name", "date_of_birth", "gender", "address", "reward_amount",
        "details", "has_document", "document_url", "detail_page_url",
        "interpol_notice_id", "link_kind", "scraped_at", "enrichment_status"]
CANONICAL_COLS = COLS[1:]  # CSV columns (no source_id; injected by loader)

UA = {"User-Agent":
      "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
      "Accept": "text/csv,application/json,*/*"}
TIMEOUT = 10


# ────────────────────────────────────────────────────────────────────────────
# Per-source fetchers

def _row(**kw):
    base = {c: "" for c in CANONICAL_COLS}
    base["scraped_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
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


def _fresh_get(url: str, **kw):
    s = requests.Session()
    s.headers.update({"Connection": "close"})
    try:
        return s.get(url, **kw)
    finally:
        s.close()


def fetch_opensanctions(name: str, agency: str, list_name: str,
                         detail_base_url: str) -> list[dict]:
    """Pull a single OS dataset → 17-col rows. One row per entity."""
    url = (f"https://data.opensanctions.org/datasets/latest/"
           f"{name}/targets.simple.csv")
    r = _fresh_get(url, headers=UA, timeout=TIMEOUT, verify=False,
                   allow_redirects=True)
    if r.status_code == 404:
        raise FileNotFoundError(f"404 {url}")
    if r.status_code != 200:
        raise RuntimeError(f"HTTP {r.status_code}")
    head = r.text[:80].lstrip("﻿").lstrip()
    if not head.startswith(('id,', '"id"')):
        raise RuntimeError(f"unexpected payload: {head[:60]!r}")
    rows = []
    rdr = csv.DictReader(io.StringIO(r.text))
    for src in rdr:
        nm = (src.get("name") or "").strip()
        if not nm or len(nm) < 2:
            continue
        ent_id = (src.get("id") or "").strip()
        aliases = (src.get("aliases") or "").strip()
        countries = (src.get("countries") or "").strip()
        addresses = (src.get("addresses") or "").strip()
        idents = (src.get("identifiers") or "").strip()
        sancs = (src.get("sanctions") or "").strip()
        birth = (src.get("birth_date") or "").strip()
        schema = (src.get("schema") or "").strip()
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
        rows.append(_row(
            source_agency=agency,
            source_list=list_name,
            case_unit=ent_id,
            name=nm,
            date_of_birth=birth[:30],
            address=addresses[:300],
            details=" | ".join(bits),
            has_document="Yes",
            detail_page_url=(
                f"https://www.opensanctions.org/entities/{ent_id}/"
                if ent_id else detail_base_url),
            link_kind="opensanctions_entity",
        ))
    return rows


def fetch_uk_ofsi(_args=None) -> list[dict]:
    url = ("https://ofsistorage.blob.core.windows.net/publishlive/"
           "2022format/ConList.csv")
    r = _fresh_get(url, headers=UA, timeout=30, verify=False,
                   allow_redirects=True)
    if r.status_code != 200:
        raise RuntimeError(f"HTTP {r.status_code}")
    # UK OFSI CSV has 2 lines of headers and varying column layout. csv.DictReader
    # against the second header row gets us the canonical columns.
    text = r.text
    # UK OFSI v2022 always emits a publication banner on row 1 (e.g.
    # "Last Updated,27/01/2026") above the real header row that starts with
    # "Name 6,Name 1,...". Skip exactly one banner line if the second line
    # looks like the canonical column header.
    lines = text.splitlines(True)
    if len(lines) >= 2 and lines[1].startswith(("Name 6,", '"Name 6"')):
        text = "".join(lines[1:])
    rdr = csv.DictReader(io.StringIO(text))
    rows = []
    for src in rdr:
        # Name columns: Name 6 is the family/last name; Name 1-5 form given names.
        bits_name = [(src.get(f"Name {i}") or "").strip() for i in range(1, 7)]
        bits_name = [b for b in bits_name if b]
        nm = " ".join(bits_name).strip()
        if not nm or len(nm) < 2:
            continue
        addr_parts = [(src.get(f"Address {i}") or "").strip()
                      for i in range(1, 7)]
        addr_parts.append((src.get("Country") or "").strip())
        addr = ", ".join(p for p in addr_parts if p)
        details_bits = []
        for k in ("DOB", "Nationality", "Listed On", "Last Updated",
                  "Regime", "Group ID", "Group Type"):
            v = (src.get(k) or "").strip()
            if v:
                details_bits.append(f"{k}: {v[:120]}")
        rows.append(_row(
            source_agency="HM Treasury / OFSI (United Kingdom)",
            source_list="Consolidated Sanctions List",
            name=nm,
            address=addr,
            date_of_birth=(src.get("DOB") or "").strip()[:30],
            details=" | ".join(details_bits),
            has_document="Yes",
            detail_page_url="https://www.gov.uk/government/publications/the-uk-sanctions-list",
            link_kind="ofsi_conlist",
        ))
    return rows


def fetch_ofac_sdn(_args=None) -> list[dict]:
    url = "https://www.treasury.gov/ofac/downloads/sdn.csv"
    r = _fresh_get(url, headers=UA, timeout=30, verify=False,
                   allow_redirects=True)
    if r.status_code != 200:
        raise RuntimeError(f"HTTP {r.status_code}")
    # SDN.csv has no header; columns:
    # 0:ent_num 1:SDN_Name 2:SDN_Type 3:Program 4:Title 5:Call_Sign
    # 6:Vess_type 7:Tonnage 8:GRT 9:Vess_flag 10:Vess_owner 11:Remarks
    rows = []
    rdr = csv.reader(io.StringIO(r.text))
    for r2 in rdr:
        if len(r2) < 2:
            continue
        ent_num = r2[0].strip()
        nm = r2[1].strip().strip('-')
        if not nm or nm.lower() == "name":
            continue
        sdn_type = (r2[2] if len(r2) > 2 else "").strip()
        program = (r2[3] if len(r2) > 3 else "").strip()
        title = (r2[4] if len(r2) > 4 else "").strip()
        remarks = (r2[11] if len(r2) > 11 else "").strip()
        details_bits = [f"OFAC ent_num: {ent_num}", f"Type: {sdn_type}",
                         f"Program: {program}"]
        if title and title != "-0-":
            details_bits.append(f"Title: {title}")
        if remarks and remarks != "-0-":
            details_bits.append(f"Remarks: {remarks[:300]}")
        rows.append(_row(
            source_agency="US Treasury OFAC",
            source_list="Specially Designated Nationals (SDN) — direct sdn.csv",
            case_unit=ent_num,
            name=nm,
            details=" | ".join(details_bits),
            has_document="Yes",
            detail_page_url="https://ofac.treasury.gov/sanctions-programs-and-country-information",
            link_kind="ofac_sdn_csv",
        ))
    return rows


def fetch_ofac_consolidated(_args=None) -> list[dict]:
    url = "https://www.treasury.gov/ofac/downloads/consolidated/cons_prim.csv"
    r = _fresh_get(url, headers=UA, timeout=20, verify=False,
                   allow_redirects=True)
    if r.status_code != 200:
        raise RuntimeError(f"HTTP {r.status_code}")
    # Same shape as sdn.csv (no header, 12 cols).
    rows = []
    rdr = csv.reader(io.StringIO(r.text))
    for r2 in rdr:
        if len(r2) < 2:
            continue
        ent_num = r2[0].strip()
        nm = r2[1].strip().strip('-')
        if not nm or nm.lower() == "name":
            continue
        sdn_type = (r2[2] if len(r2) > 2 else "").strip()
        program = (r2[3] if len(r2) > 3 else "").strip()
        remarks = (r2[11] if len(r2) > 11 else "").strip()
        details = (f"OFAC ent_num: {ent_num} | Type: {sdn_type} | "
                   f"Program: {program} | "
                   f"Remarks: {remarks[:300] if remarks not in ('-0-','') else ''}")
        rows.append(_row(
            source_agency="US Treasury OFAC",
            source_list="Consolidated Non-SDN List (primary) — direct cons_prim.csv",
            case_unit=ent_num,
            name=nm,
            details=details,
            has_document="Yes",
            detail_page_url="https://ofac.treasury.gov/consolidated-sanctions-list",
            link_kind="ofac_consolidated_csv",
        ))
    return rows


def fetch_bis_entity_list(_args=None) -> list[dict]:
    url = ("https://www.bis.doc.gov/index.php/policy-guidance/"
           "lists-of-parties-of-concern/entity-list/file/"
           "1064-el-entity-list-csv")
    r = _fresh_get(url, headers=UA, timeout=15, verify=False,
                   allow_redirects=True)
    if r.status_code != 200:
        raise RuntimeError(f"HTTP {r.status_code}")
    rdr = csv.DictReader(io.StringIO(r.text))
    rows = []
    for src in rdr:
        nm = (src.get("Entity") or src.get("Name") or "").strip()
        if not nm or len(nm) < 2:
            continue
        country = (src.get("Country") or "").strip()
        addr = (src.get("Address") or "").strip()
        license_req = (src.get("License Requirement") or "").strip()
        fr_citation = (src.get("Federal Register Citation") or "").strip()
        details_bits = []
        if country: details_bits.append(f"Country: {country}")
        if license_req: details_bits.append(f"License Req: {license_req[:120]}")
        if fr_citation: details_bits.append(f"FR Citation: {fr_citation[:120]}")
        rows.append(_row(
            source_agency="US Bureau of Industry and Security (BIS)",
            source_list="Entity List — direct CSV",
            name=nm,
            address=addr,
            details=" | ".join(details_bits),
            has_document="Yes",
            detail_page_url=("https://www.bis.doc.gov/index.php/policy-guidance/"
                             "lists-of-parties-of-concern/entity-list"),
            link_kind="bis_entity_list_csv",
        ))
    return rows


# ────────────────────────────────────────────────────────────────────────────
# Registry: (source_id, fetcher, args, agency, list_name)

DIRECT_SOURCES = [
    ("uk_ofsi_conlist_csv", fetch_uk_ofsi, None,
     "HM Treasury / OFSI (United Kingdom)",
     "Consolidated Sanctions List — direct OFSI CSV"),
    ("ofac_sdn_direct_csv", fetch_ofac_sdn, None,
     "US Treasury OFAC",
     "SDN List — direct sdn.csv"),
    ("ofac_consolidated_primary_csv", fetch_ofac_consolidated, None,
     "US Treasury OFAC",
     "Consolidated Non-SDN List — direct cons_prim.csv"),
    ("bis_entity_list_direct_csv", fetch_bis_entity_list, None,
     "US Bureau of Industry and Security (BIS)",
     "Entity List — direct CSV"),
]

OS_FEEDS = [
    # (source_id used in DB, OS dataset name, agency, list_name)
    ("za_fic_sanctions", "za_fic_sanctions",
     "South Africa Financial Intelligence Centre (FIC)",
     "Targeted Financial Sanctions"),
    ("il_mod_terrorists", "il_mod_terrorists",
     "Israel Ministry of Defense",
     "Designated Terrorist Organisations"),
    ("ir_sanctions", "ir_sanctions",
     "Iran Sanctions (domestic register)",
     "Sanctions Targets"),
    ("ru_nsd_isin", "ru_nsd_isin",
     "Russia National Settlement Depository (NSD)",
     "Sanctioned ISINs"),
    ("kz_afmrk_sanctions", "kz_afmrk_sanctions",
     "Kazakhstan AFMRK",
     "Sanctioned Persons"),
    ("ua_nsdc_sanctions", "ua_nsdc_sanctions",
     "Ukraine National Security and Defence Council (NSDC)",
     "Sanctioned Persons"),
    ("ua_sfms_blacklist", "ua_sfms_blacklist",
     "Ukraine State Financial Monitoring Service (SFMS)",
     "Blacklist"),
    ("ru_fedsfm_terror", "ru_fedsfm_terror",
     "Russia Federal Financial Monitoring Service (Rosfinmonitoring)",
     "Terrorist / Extremist List"),
    ("gb_coh_disqualified", "gb_coh_disqualified",
     "UK Companies House",
     "Disqualified Directors"),
    ("gb_fcdo_sanctions", "gb_fcdo_sanctions",
     "UK Foreign, Commonwealth & Development Office (FCDO)",
     "UK Sanctions List"),
    ("us_sec_harmed_investors", "us_sec_harmed_investors",
     "US Securities and Exchange Commission (SEC)",
     "Harmed Investors"),
    ("ge_declarations", "ge_declarations",
     "Georgia Declarations Registry",
     "Public Official Declarations"),
    ("afdb_sanctions", "afdb_sanctions",
     "African Development Bank",
     "Sanctioned Firms and Individuals"),
    ("us_cia_world_leaders", "us_cia_world_leaders",
     "US CIA",
     "Chiefs of State and Cabinet Members of Foreign Governments"),
    ("fr_senat", "fr_senat",
     "Sénat de la République française",
     "Senators (PEPs)"),
    ("nl_senate", "nl_senate",
     "Netherlands Senate (Eerste Kamer)",
     "Senate Members (PEPs)"),
    ("de_bundestag", "de_bundestag",
     "Deutscher Bundestag",
     "Members of the Bundestag (PEPs)"),
    ("at_nationalrat", "at_nationalrat",
     "Österreichischer Nationalrat",
     "National Council Members (PEPs)"),
    ("ie_oireachtas", "ie_oireachtas",
     "Houses of the Oireachtas (Ireland)",
     "Members of the Oireachtas (PEPs)"),
    ("dk_folketing", "dk_folketing",
     "Folketinget (Denmark)",
     "Members of the Folketing (PEPs)"),
    ("fi_eduskunta", "fi_eduskunta",
     "Eduskunta (Finland)",
     "Members of the Eduskunta (PEPs)"),
    ("se_riksdagen", "se_riksdagen",
     "Sveriges Riksdag",
     "Members of the Riksdag (PEPs)"),
    ("no_storting", "no_storting",
     "Stortinget (Norway)",
     "Members of the Storting (PEPs)"),
    ("lt_seimas_os", "lt_seimas",
     "Seimas of the Republic of Lithuania",
     "Members of the Seimas (PEPs)"),
    ("lv_saeima", "lv_saeima",
     "Saeima (Latvia)",
     "Members of the Saeima (PEPs)"),
    ("ee_riigikogu", "ee_riigikogu",
     "Riigikogu (Estonia)",
     "Members of the Riigikogu (PEPs)"),
    ("hr_sabor", "hr_sabor",
     "Hrvatski Sabor",
     "Members of the Sabor (PEPs)"),
    ("rs_parliament", "rs_parliament",
     "Narodna skupština Republike Srbije",
     "Members of the National Assembly (PEPs)"),
    ("bg_parliament", "bg_parliament",
     "Narodno sabranie (Bulgaria)",
     "Members of the National Assembly (PEPs)"),
    ("sk_nrsr", "sk_nrsr",
     "Národná rada Slovenskej republiky",
     "National Council Members (PEPs)"),
    ("cz_senate", "cz_senate",
     "Senát Parlamentu České republiky",
     "Senators (PEPs)"),
    ("hu_parliament", "hu_parliament",
     "Magyar Országgyűlés",
     "National Assembly Members (PEPs)"),
    ("ro_cdep", "ro_cdep",
     "Camera Deputaților (Romania)",
     "Chamber of Deputies Members (PEPs)"),
    ("pl_sejm", "pl_sejm",
     "Sejm of the Republic of Poland",
     "Members of the Sejm (PEPs)"),
    ("cy_parliament", "cy_parliament",
     "House of Representatives of Cyprus",
     "Members of Parliament (PEPs)"),
    ("mt_parliament", "mt_parliament",
     "Parlament ta' Malta",
     "Members of Parliament (PEPs)"),
    ("is_althingi", "is_althingi",
     "Alþingi (Iceland)",
     "Members of the Althingi (PEPs)"),
    ("md_parliament", "md_parliament",
     "Parlamentul Republicii Moldova",
     "Members of Parliament (PEPs)"),
    ("me_skupstina", "me_skupstina",
     "Skupština Crne Gore",
     "Members of the Assembly (PEPs)"),
    ("al_parliament", "al_parliament",
     "Kuvendi i Shqipërisë",
     "Members of the Assembly (PEPs)"),
    ("mk_parliament", "mk_parliament",
     "Sobranie na Republika Severna Makedonija",
     "Members of the Assembly (PEPs)"),
    ("ba_parliament", "ba_parliament",
     "Parlamentarna skupština Bosne i Hercegovine",
     "Members of the Parliamentary Assembly (PEPs)"),
]


# ────────────────────────────────────────────────────────────────────────────
# DB helpers

def _existing_source_ids(conn) -> set[str]:
    with conn.cursor() as cur:
        cur.execute("SELECT DISTINCT source_id FROM watchlist_records "
                    "WHERE source_id IS NOT NULL AND source_id != '';")
        return {r[0] for r in cur.fetchall()}


def _insert_to_db(conn, sid: str, rows: list[dict]) -> int:
    if not rows:
        return 0
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


def _register(sources_data: dict, sid: str, agency: str, list_name: str,
              url: str) -> bool:
    have = {s["id"] for s in sources_data["sources"]}
    if sid in have:
        # Mark active if not already.
        for s in sources_data["sources"]:
            if s["id"] == sid and s.get("status") != "active":
                s["status"] = "active"
                return True
        return False
    sources_data["sources"].append({
        "id": sid, "agency": agency, "list_name": list_name, "url": url,
        "type": "html", "scraper": None, "expected_min_records": 0,
        "status": "active", "change_detection": False,
        "change_detection_selector": None,
        "country": "International",
        "notes": "Bulk-loaded via scripts/fetch_remaining_sources.py.",
    })
    return True


# ────────────────────────────────────────────────────────────────────────────
# Main

def main():
    conn = psycopg2.connect(**LOCAL_DB)
    conn.autocommit = False
    existing = _existing_source_ids(conn)
    print(f"DB already has {len(existing)} sources with data.", flush=True)

    with open(SOURCES_JSON) as f:
        sources_data = json.load(f)

    summary = []
    t_total = time.time()

    # 1) Direct CSVs.
    for sid, fetcher, _args, agency, list_name in DIRECT_SOURCES:
        if sid in existing:
            print(f"  SKIP {sid} — already in DB", flush=True)
            continue
        t0 = time.time()
        try:
            rows = fetcher()
        except Exception as e:
            print(f"  FAIL [{sid}] {type(e).__name__}: {str(e)[:120]}",
                  flush=True)
            summary.append({"id": sid, "status": "fail",
                            "error": f"{type(e).__name__}: {str(e)[:200]}"})
            continue
        if not rows:
            print(f"  EMPTY [{sid}]", flush=True)
            summary.append({"id": sid, "status": "empty"})
            continue
        out = os.path.join(DATA_DIR, f"{sid}.csv")
        _write_csv(rows, out)
        n = _insert_to_db(conn, sid, rows)
        _register(sources_data, sid, agency, list_name,
                  rows[0].get("detail_page_url", ""))
        existing.add(sid)
        elapsed = time.time() - t0
        print(f"  ✅ [{sid}] +{n:,} rows | Progress: {len(existing)}/800 "
              f"({elapsed:.1f}s)", flush=True)
        summary.append({"id": sid, "status": "success", "rows": n,
                        "elapsed_s": round(elapsed, 1)})

    # 2) OpenSanctions per-jurisdiction feeds.
    for sid, ds_name, agency, list_name in OS_FEEDS:
        if sid in existing:
            print(f"  SKIP {sid} — already in DB", flush=True)
            continue
        t0 = time.time()
        try:
            rows = fetch_opensanctions(
                ds_name, agency, list_name,
                detail_base_url=f"https://www.opensanctions.org/datasets/{ds_name}/")
        except FileNotFoundError as e:
            print(f"  404 [{sid}]", flush=True)
            summary.append({"id": sid, "status": "404"})
            continue
        except Exception as e:
            print(f"  FAIL [{sid}] {type(e).__name__}: {str(e)[:120]}",
                  flush=True)
            summary.append({"id": sid, "status": "fail",
                            "error": f"{type(e).__name__}: {str(e)[:200]}"})
            continue
        if not rows:
            print(f"  EMPTY [{sid}]", flush=True)
            summary.append({"id": sid, "status": "empty"})
            continue
        out = os.path.join(DATA_DIR, f"{sid}.csv")
        _write_csv(rows, out)
        n = _insert_to_db(conn, sid, rows)
        _register(sources_data, sid, agency, list_name,
                  f"https://www.opensanctions.org/datasets/{ds_name}/")
        existing.add(sid)
        elapsed = time.time() - t0
        print(f"  ✅ [{sid}] +{n:,} rows | Progress: {len(existing)}/800 "
              f"({elapsed:.1f}s)", flush=True)
        summary.append({"id": sid, "status": "success", "rows": n,
                        "elapsed_s": round(elapsed, 1)})

    conn.close()

    with open(SOURCES_JSON, "w") as f:
        json.dump(sources_data, f, indent=2)

    with open(SUMMARY_PATH, "w") as f:
        json.dump({"finished_at": datetime.now(timezone.utc).isoformat(),
                   "results": summary}, f, indent=2)

    n_ok = sum(1 for r in summary if r["status"] == "success")
    n_404 = sum(1 for r in summary if r["status"] == "404")
    n_fail = sum(1 for r in summary if r["status"] == "fail")
    total_rows = sum(r.get("rows", 0) for r in summary)
    print(f"\n=== fetch_remaining_sources summary ({time.time()-t_total:.1f}s) ===")
    print(f"  success: {n_ok}")
    print(f"  404:     {n_404}")
    print(f"  fail:    {n_fail}")
    print(f"  rows:    {total_rows:,}")
    print(f"  with-data sources in DB: {len(existing)} / 800")


if __name__ == "__main__":
    sys.exit(main())
