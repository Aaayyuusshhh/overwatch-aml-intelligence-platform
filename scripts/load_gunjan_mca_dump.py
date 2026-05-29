#!/usr/bin/env python3
"""
scripts/load_gunjan_mca_dump.py — one-shot ingest of Gunjan's MCA MongoDB
export into our 17-column canonical schema, then per-source targeted refresh
of watchlist_records on both local Postgres and RDS.

Input:  data/mcaDefaulterData_Negative List.json  (1.46M records, ~2GB)
Output: data/<source_id>.csv per source (canonical 17 cols, no source_id)
        plus per-source DELETE+INSERT into watchlist_records on local + RDS.

The dump's `source` string field maps to our internal source_ids:

  MCA Defaulter Companies     -> mca_defaulter_companies        (~614K)
  MCA Disqualified Directors  -> mca_disqualified_directors_164 (~565K)
  MCA Struck off Company      -> mca_companies_struck_off       (~204K)
  MCA Defaulter Directors     -> mca_defaulter_directors         (~77K)
  MCA Proclaimed Director     -> mca_proclaimed_offenders          (259)
  MCA Vanishing Company       -> mca_vanishing_companies           (199)
  MCA MLM Company             -> mca_mlm_companies      (NEW)      (89)
  MCA Defaulter Secretaries   -> mca_defaulter_secretaries  (NEW)  (82)

The dump's 199 vanishing-company rows are *smaller* than the 903 rows we
already scraped from MCA's website. We never overwrite that source — its
CSV stays as-is and we route the dump's vanishing rows to a sidecar
data/gunjan_mca_vanishing_companies.csv for human review only.

CLI:
  load_gunjan_mca_dump.py --transform                # write CSVs only
  load_gunjan_mca_dump.py --load local               # DELETE+INSERT local DB
  load_gunjan_mca_dump.py --load rds                 # DELETE+INSERT RDS
  load_gunjan_mca_dump.py --load both                # do both (default after transform)
  load_gunjan_mca_dump.py --all                      # transform + load both
  load_gunjan_mca_dump.py --verify                   # print row counts pre/post
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import sys
import time
from collections import Counter
from typing import Optional

import psycopg2
import psycopg2.extras

try:
    import ijson
except ImportError:
    print("FATAL: ijson not installed. Run: ./venv/bin/pip install ijson",
          file=sys.stderr)
    sys.exit(1)

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.path.join(PROJECT_ROOT, "data")
INPUT_JSON = os.path.join(DATA_DIR, "mcaDefaulterData_Negative List.json")
SOURCES_JSON = os.path.join(PROJECT_ROOT, "sources.json")

# Dump's "source" field -> our source_id + canonical list_name.
SOURCE_MAP = {
    "MCA Defaulter Companies":    ("mca_defaulter_companies",
                                    "Defaulter Companies (Filing Default)"),
    "MCA Disqualified Directors": ("mca_disqualified_directors_164",
                                    "Disqualified Directors U/S 164(2)(A)"),
    "MCA Struck off Company":     ("mca_companies_struck_off",
                                    "Companies Struck Off (STK-7) U/S 248(5)"),
    "MCA Defaulter Directors":    ("mca_defaulter_directors",
                                    "Defaulter Directors (Filing Default)"),
    "MCA Proclaimed Director":    ("mca_proclaimed_offenders",
                                    "Proclaimed Offenders U/S 82 Cr.PC"),
    "MCA Vanishing Company":      ("mca_vanishing_companies",
                                    "Vanishing Companies (MCA via WatchOut)"),
    "MCA MLM Company":            ("mca_mlm_companies",
                                    "MLM / Money-Circulation Companies (Negative List)"),
    "MCA Defaulter Secretaries":  ("mca_defaulter_secretaries",
                                    "Defaulter Company Secretaries (Filing Default)"),
}

# Skip overwriting this source's CSV / DB rows — we already have richer
# web-scraped data (903 rows vs the dump's 199). The dump rows go to a
# sidecar for review.
KEEP_EXISTING = {"mca_vanishing_companies"}

# New sources we need to register in sources.json the first time we run.
NEW_SOURCES = {
    "mca_mlm_companies": {
        "id": "mca_mlm_companies",
        "agency": "Ministry of Corporate Affairs (MCA)",
        "list_name": "MLM / Money-Circulation Companies (Negative List)",
        "url": None,
        "type": "direct_bulk",
        "scraper": None,
        "expected_min_records": 80,
        "status": "active",
        "change_detection": False,
        "change_detection_selector": None,
        "country": "India",
        "notes": ("Loaded from Gunjan's MCA MongoDB dump 2026-05-28. "
                  "No live web source — refresh via dump re-import."),
    },
    "mca_defaulter_secretaries": {
        "id": "mca_defaulter_secretaries",
        "agency": "Ministry of Corporate Affairs (MCA)",
        "list_name": "Defaulter Company Secretaries (Filing Default)",
        "url": None,
        "type": "direct_bulk",
        "scraper": None,
        "expected_min_records": 75,
        "status": "active",
        "change_detection": False,
        "change_detection_selector": None,
        "country": "India",
        "notes": ("Loaded from Gunjan's MCA MongoDB dump 2026-05-28. "
                  "No live web source — refresh via dump re-import."),
    },
}

CANONICAL_COLUMNS = [
    "source_agency", "source_list", "case_unit",
    "name", "father_name", "date_of_birth", "gender",
    "address", "reward_amount", "details",
    "has_document", "document_url", "detail_page_url",
    "interpol_notice_id", "link_kind", "scraped_at",
    "enrichment_status",
]

# What we feed psycopg2.execute_values for the targeted refresh.
INSERT_COLUMNS = ["source_id"] + CANONICAL_COLUMNS

AGENCY = "Ministry of Corporate Affairs (MCA)"

DB_LOCAL = dict(host=os.environ.get("PG_HOST", "localhost"),
                user=os.environ.get("PG_USER", "aayush"),
                password=os.environ.get("PG_PASSWORD", "aayush123"),
                dbname=os.environ.get("PG_DB", "risk_pipeline"),
                connect_timeout=30)
DB_RDS = dict(host="overwatch-aml.cnsgg0i0cxa7.ap-south-1.rds.amazonaws.com",
              user="aayush", password="Aaayyuusshhh",
              dbname="risk_pipeline", connect_timeout=60,
              keepalives=1, keepalives_idle=15, keepalives_interval=5,
              keepalives_count=10,
              # 30-min statement timeout. RDS db.t4g.micro updates the
              # GIN trigram index on `name` and `details` synchronously
              # inside the INSERT — for sources with long details strings
              # (mca_companies_struck_off packs CIN + ROC + status + ...
              # into ~500 bytes per row) a 25K-row chunk can take many
              # minutes. 5 min was too aggressive; 30 min is safe.
              options="-c statement_timeout=1800000")

# When loading to RDS, commit after every CHUNK rows. Smaller chunks =
# more roundtrips but each one fits comfortably under statement_timeout
# even when GIN indexes are slow, and a TCP drop loses less work. The
# 950K-row defaulter case proved that 50K chunks can run hot; 10K leaves
# headroom for the GIN trigram update.
RDS_CHUNK_ROWS = 10_000


# ---------------------------------------------------------------------------
# Per-record transform

def _safe(v) -> str:
    """Normalise a JSON value into a stripped string. MongoDB nulls become ''.
    Lists/dicts become JSON so we never silently lose data."""
    if v is None:
        return ""
    if isinstance(v, (list, dict)):
        return json.dumps(v, ensure_ascii=False)
    s = str(v).strip()
    # Some fields are MongoDB extended-JSON: {"$date": "..."} or
    # {"$oid": "..."}. Unwrap to a plain string if so.
    return s


def _pick_name(rec: dict) -> str:
    """Defaulter entities come in three flavours: a person row (name set),
    a company row (companyName set), or both (rare). Prefer person, fall back
    to company; defaulterName is a denormalised "subject of the action" field
    used in some rows."""
    for k in ("name", "defaulterName", "companyName"):
        v = _safe(rec.get(k))
        if v:
            return v
    return ""


def _pick_address(rec: dict) -> str:
    parts = [_safe(rec.get("registeredAddress")),
             _safe(rec.get("state"))]
    return ", ".join(p for p in parts if p)


def _build_details(rec: dict) -> str:
    """Pack the structured MCA metadata into one readable details string.
    Lossless: every non-empty field below ends up in the search corpus, so
    a screening match on e.g. CIN still surfaces the row."""
    bits = []
    # Identity
    cin = _safe(rec.get("cinNumber"))
    pan = _safe(rec.get("companyPan"))
    company = _safe(rec.get("companyName"))
    if cin:     bits.append(f"CIN: {cin}")
    if pan:     bits.append(f"PAN: {pan}")
    if company and company != _pick_name(rec):
        bits.append(f"Company: {company}")
    # Registration / classification
    for label, key in (("ROC", "rocName"),
                       ("Year", "year"),
                       ("Defaulting Year", "defaultingYear"),
                       ("Risk Category", "riskCategory"),
                       ("Case Category", "caseCategory"),
                       ("Case Status", "caseStatus"),
                       ("Company Status", "companyStatus"),
                       ("Company Type", "companyType"),
                       ("Company Sub-category", "companySubCategory"),
                       ("Listed/Unlisted", "listUnlistStatus"),
                       ("Industry Code", "companyIndustrialCode"),
                       ("Industry Type", "companyIndustrialType"),
                       ("Authorised Capital", "authorisedCapital"),
                       ("Paid-Up Capital", "paidUpCapital"),
                       ("Incorporation Date", "incorporationDate"),
                       ("Petitioner", "petitioner"),
                       ("Respondent", "respondent"),
                       ("SRN", "srn"),
                       ("Signature ID", "signatureId"),
                       ("Source File", "fileName")):
        v = _safe(rec.get(key))
        if v:
            bits.append(f"{label}: {v}")
    return " | ".join(bits)


def _pick_scraped_at(rec: dict) -> str:
    """Prefer the dump's normalised dd/mm/yyyy date, else uploadedDate (also
    dd-mm-yyyy in the data), else created_date which is a Java toString."""
    for k in ("updated_date_format", "uploadedDate", "updated_date",
              "created_date"):
        v = _safe(rec.get(k))
        if v:
            return v
    return ""


def transform_row(rec: dict, list_name: str) -> dict:
    name = _pick_name(rec)
    link = _safe(rec.get("link"))
    return {
        "source_agency":      AGENCY,
        "source_list":        list_name,
        "case_unit":          _safe(rec.get("cinNumber")) or _safe(rec.get("companyPan")),
        "name":               name,
        "father_name":        "",
        "date_of_birth":      "",
        "gender":             "",
        "address":            _pick_address(rec),
        "reward_amount":      "",
        "details":            _build_details(rec),
        "has_document":       "Yes" if link else "No",
        "document_url":       "",
        "detail_page_url":    link,
        "interpol_notice_id": "",
        "link_kind":          "mca_case" if link else "",
        "scraped_at":         _pick_scraped_at(rec),
        "enrichment_status":  "",
    }


# ---------------------------------------------------------------------------
# Streaming transform

def stream_transform(input_path: str, verbose: bool = True) -> dict:
    """Read the JSON array record-by-record, route each row to the right
    per-source CSV. Returns {source_id: row_count_written}."""
    # Pre-open one csv.writer per source_id we'll touch. Vanishing-company
    # rows go to a sidecar file (not the canonical data/mca_vanishing_companies.csv)
    # so the existing 903-row scrape stays untouched.
    writers: dict[str, tuple] = {}  # sid -> (filehandle, csv.writer)
    output_paths: dict[str, str] = {}
    for src_label, (sid, list_name) in SOURCE_MAP.items():
        if sid in KEEP_EXISTING:
            out_path = os.path.join(DATA_DIR, f"gunjan_{sid}.csv")
        else:
            out_path = os.path.join(DATA_DIR, f"{sid}.csv")
        output_paths[sid] = out_path
        # Open + write header right away so partial runs leave valid CSVs.
        fh = open(out_path, "w", encoding="utf-8", newline="")
        w = csv.DictWriter(fh, fieldnames=CANONICAL_COLUMNS,
                           extrasaction="ignore")
        w.writeheader()
        writers[sid] = (fh, w)

    counts: Counter = Counter()
    skipped_unknown = 0
    skipped_no_name = 0
    t0 = time.time()
    last_log = t0

    with open(input_path, "rb") as f:
        for rec in ijson.items(f, "item"):
            src_label = rec.get("source")
            if src_label not in SOURCE_MAP:
                skipped_unknown += 1
                continue
            sid, list_name = SOURCE_MAP[src_label]
            row = transform_row(rec, list_name)
            if not row["name"]:
                # No identity at all — neither person, company, nor defaulter
                # name. Without a name nothing can match it on screening, so
                # drop with a count (we'll report it).
                skipped_no_name += 1
                continue
            _, w = writers[sid]
            w.writerow(row)
            counts[sid] += 1
            now = time.time()
            if verbose and (now - last_log) > 5:
                total = sum(counts.values())
                rate = total / max(1, now - t0)
                print(f"  ... wrote {total:>9,} rows  ({rate:,.0f}/s)",
                      flush=True)
                last_log = now

    for fh, _ in writers.values():
        fh.close()

    elapsed = time.time() - t0
    print(f"\ntransform complete in {elapsed:.1f}s "
          f"({sum(counts.values()):,} rows written, "
          f"skipped {skipped_unknown:,} unknown-source, "
          f"{skipped_no_name:,} no-name)")
    for sid, n in counts.most_common():
        marker = "  (SIDECAR — not loaded)" if sid in KEEP_EXISTING else ""
        print(f"  {sid:40s} {n:>8,} → {output_paths[sid]}{marker}")
    return {"counts": dict(counts),
            "paths": output_paths,
            "skipped_unknown": skipped_unknown,
            "skipped_no_name": skipped_no_name,
            "elapsed_s": round(elapsed, 1)}


# ---------------------------------------------------------------------------
# sources.json registration

def register_new_sources() -> int:
    """Idempotent: only inserts entries that don't already exist by id."""
    with open(SOURCES_JSON) as f:
        data = json.load(f)
    existing_ids = {s.get("id") for s in data.get("sources", [])}
    added = 0
    for sid, entry in NEW_SOURCES.items():
        if sid in existing_ids:
            continue
        data.setdefault("sources", []).append(entry)
        added += 1
        print(f"  + registered new source: {sid}")
    if added:
        tmp = SOURCES_JSON + ".tmp"
        with open(tmp, "w") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
        os.replace(tmp, SOURCES_JSON)
        print(f"  wrote {SOURCES_JSON} (+{added} new entries, "
              f"{len(data['sources'])} total)")
    else:
        print("  no new sources to register (already present)")
    return added


# ---------------------------------------------------------------------------
# DB load

def _csv_rows_for_db(path: str, sid: str) -> list[tuple]:
    """Read the CSV produced by stream_transform and yield rows in
    INSERT_COLUMNS order. We read into memory because execute_values wants
    a sequence — but each source file is bounded (largest is ~614K rows,
    ~250MB) and runs on machines with 15GB+ RAM."""
    rows: list[tuple] = []
    with open(path, encoding="utf-8", newline="") as f:
        reader = csv.DictReader(f)
        for r in reader:
            rows.append((
                sid,
                r.get("source_agency") or "",
                r.get("source_list") or "",
                r.get("case_unit") or "",
                r.get("name") or "",
                r.get("father_name") or "",
                r.get("date_of_birth") or "",
                r.get("gender") or "",
                r.get("address") or "",
                r.get("reward_amount") or "",
                r.get("details") or "",
                r.get("has_document") or "",
                r.get("document_url") or "",
                r.get("detail_page_url") or "",
                r.get("interpol_notice_id") or "",
                r.get("link_kind") or "",
                r.get("scraped_at") or "",
                r.get("enrichment_status") or "",
            ))
    return rows


def _db_count(conn, sid: str) -> int:
    with conn.cursor() as cur:
        cur.execute("SELECT COUNT(*) FROM watchlist_records WHERE source_id = %s;",
                    (sid,))
        return int(cur.fetchone()[0])


def _copy_load_source(conn, sid: str, csv_path: str,
                      reconnect_kw: Optional[dict]) -> dict:
    """RDS-optimised path: DELETE + COPY FROM STDIN in one transaction.

    Why COPY beats execute_values on RDS:
      * No SQL-level batching — one big stream over one socket
      * No per-row escaping in Python — the CSV is pumped almost verbatim
      * ~5-10x faster than execute_values for our row size
      * Atomic with the preceding DELETE — TCP drop means full rollback
        back to the source's pre-load state. No "lost 614K rows" failure
        mode; the retry path is simply "re-run, the rollback restored us".

    Memory: we materialise the rewritten CSV (with source_id prepended)
    in a single StringIO. For mca_defaulter_companies that's ~250MB of
    Python string — fits comfortably in 15GB.
    """
    import io
    t0 = time.time()

    # Build the COPY payload. We rewrite the CSV with source_id as the first
    # column so COPY can land it directly without a temp table.
    buf = io.StringIO()
    w = csv.writer(buf)
    n = 0
    with open(csv_path, encoding="utf-8", errors="replace", newline="") as f:
        reader = csv.DictReader(f)
        for r in reader:
            w.writerow([
                sid,
                r.get("source_agency", "") or "",
                r.get("source_list", "") or "",
                r.get("case_unit", "") or "",
                r.get("name", "") or "",
                r.get("father_name", "") or "",
                r.get("date_of_birth", "") or "",
                r.get("gender", "") or "",
                r.get("address", "") or "",
                r.get("reward_amount", "") or "",
                r.get("details", "") or "",
                r.get("has_document", "") or "",
                r.get("document_url", "") or "",
                r.get("detail_page_url", "") or "",
                r.get("interpol_notice_id", "") or "",
                r.get("link_kind", "") or "",
                r.get("scraped_at", "") or "",
                r.get("enrichment_status", "") or "",
            ])
            n += 1
    buf_bytes = buf.getvalue()
    print(f"    [{sid}] prepared COPY payload: {n:,} rows, "
          f"{len(buf_bytes)/1024/1024:.1f}MB", flush=True)

    copy_sql = (
        f"COPY watchlist_records ({','.join(INSERT_COLUMNS)}) "
        "FROM STDIN WITH (FORMAT csv)"
    )

    # Retry loop: TCP drop = ROLLBACK = retry whole DELETE+COPY transaction.
    attempt = 0
    while True:
        attempt += 1
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT COUNT(*) FROM watchlist_records WHERE source_id = %s;",
                    (sid,))
                pre = int(cur.fetchone()[0])
                cur.execute(
                    "DELETE FROM watchlist_records WHERE source_id = %s;",
                    (sid,))
                deleted = cur.rowcount
                cur.copy_expert(copy_sql, io.StringIO(buf_bytes))
                cur.execute(
                    "SELECT COUNT(*) FROM watchlist_records WHERE source_id = %s;",
                    (sid,))
                post = int(cur.fetchone()[0])
            conn.commit()
            return {"sid": sid, "pre": pre, "deleted": deleted,
                    "inserted": n, "post": post,
                    "elapsed_s": round(time.time() - t0, 1),
                    "method": "copy",
                    "conn": conn}
        except (psycopg2.OperationalError, psycopg2.InterfaceError) as e:
            print(f"    [{sid}] COPY attempt {attempt} dropped TCP "
                  f"({str(e)[:160]}) — rolling back & retrying",
                  file=sys.stderr, flush=True)
            try:
                conn.rollback()
            except Exception:
                pass
            try:
                conn.close()
            except Exception:
                pass
            if not reconnect_kw:
                raise
            # Infinite reconnect retry with back-off — daily cron above us
            # owns the wall-clock cap.
            while True:
                try:
                    conn = psycopg2.connect(**reconnect_kw)
                    conn.autocommit = False
                    break
                except Exception as inner:
                    print(f"    [{sid}] reconnect failed "
                          f"({str(inner)[:120]}) — sleeping 30s",
                          file=sys.stderr, flush=True)
                    time.sleep(30)


def load_one_source(conn, sid: str, csv_path: str, *,
                    chunk_rows: int = 0,
                    reconnect_kw: Optional[dict] = None) -> dict:
    """DELETE + chunked INSERT for a single source.

    chunk_rows=0 means one big execute_values call (fine for local). For
    RDS use RDS_CHUNK_ROWS — each chunk commits, so a TCP drop loses at
    most one chunk's worth of work, and the caller's --skip-if-loaded
    path can fast-forward right past sources whose post-count already
    matches the CSV.

    reconnect_kw, if provided, lets us auto-reopen the connection mid-load
    when RDS drops us. Without it we just raise.
    """
    t0 = time.time()
    rows = _csv_rows_for_db(csv_path, sid)
    n = len(rows)
    if n == 0:
        return {"sid": sid, "skipped": True, "reason": "no rows in CSV"}

    cols = ",".join(INSERT_COLUMNS)
    insert_sql = f"INSERT INTO watchlist_records ({cols}) VALUES %s"

    def _reopen():
        if not reconnect_kw:
            raise
        print(f"    [{sid}] reconnecting to {reconnect_kw['host']} ...")
        new = psycopg2.connect(**reconnect_kw)
        new.autocommit = False
        return new

    # Capture pre-count (read-only, safe to retry).
    pre = -1
    while True:
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT COUNT(*) FROM watchlist_records WHERE source_id = %s;",
                    (sid,))
                pre = int(cur.fetchone()[0])
            break
        except (psycopg2.OperationalError, psycopg2.InterfaceError) as e:
            print(f"    [{sid}] pre-count dropped TCP ({str(e)[:120]}) — reconnecting",
                  file=sys.stderr, flush=True)
            try:
                conn.close()
            except Exception:
                pass
            conn = _reopen()

    deleted = -1

    if chunk_rows <= 0 or n <= chunk_rows:
        # Single-batch path: DELETE+INSERT in one transaction. If TCP
        # drops we lose everything — but that's safe (rolls back to the
        # pre-load state) and the retry path re-runs from scratch.
        while True:
            try:
                with conn.cursor() as cur:
                    cur.execute(
                        "DELETE FROM watchlist_records WHERE source_id = %s;",
                        (sid,))
                    deleted = cur.rowcount
                    psycopg2.extras.execute_values(
                        cur, insert_sql, rows, page_size=5000)
                conn.commit()
                break
            except (psycopg2.OperationalError, psycopg2.InterfaceError) as e:
                print(f"    [{sid}] single-batch dropped TCP "
                      f"({str(e)[:120]}) — reconnecting & retrying whole load",
                      file=sys.stderr, flush=True)
                try:
                    conn.close()
                except Exception:
                    pass
                conn = _reopen()
    else:
        inserted_so_far = 0
        i = 0
        # The first chunk's transaction ALSO carries the DELETE, so the
        # window between "old data gone" and "new data starting to land"
        # is zero. If TCP drops during chunk 0, Postgres rolls back the
        # DELETE — RDS state stays at `pre` and the retry path runs again.
        # Only after chunk 0 commits is the source officially "in-flight";
        # subsequent chunks commit independently and a drop only loses
        # one chunk's worth of rows (auto-resumed by the reconnect logic).
        delete_pending = True
        while i < n:
            chunk = rows[i:i + chunk_rows]
            try:
                with conn.cursor() as cur:
                    if delete_pending:
                        cur.execute(
                            "DELETE FROM watchlist_records WHERE source_id = %s;",
                            (sid,))
                        deleted = cur.rowcount
                    psycopg2.extras.execute_values(
                        cur, insert_sql, chunk, page_size=5000)
                conn.commit()
                if delete_pending:
                    delete_pending = False
                    print(f"    [{sid}] DELETE+chunk-0 committed "
                          f"(deleted={deleted:,}, chunk={len(chunk):,})",
                          flush=True)
                inserted_so_far += len(chunk)
                i += len(chunk)
                if (i // chunk_rows) % 5 == 0 or i >= n:
                    print(f"    [{sid}] inserted {inserted_so_far:>7,}/{n:,} "
                          f"({time.time()-t0:.1f}s elapsed)", flush=True)
            except (psycopg2.OperationalError, psycopg2.InterfaceError) as e:
                # The TCP drop could have happened EITHER before COMMIT (so
                # the chunk rolled back and we must retry) OR after COMMIT
                # but before the OK reached us (so the chunk is already in
                # the DB — retrying would duplicate it). After every drop
                # we reopen the connection, ask Postgres for the current
                # row count, and compare against what we KNOW we'd written.
                # Only advance if the committed count proves this chunk made it.
                print(f"    [{sid}] OperationalError at row {i:,}: "
                      f"{str(e)[:200]}", file=sys.stderr, flush=True)
                try:
                    conn.close()
                except Exception:
                    pass
                # _reopen retries forever — never give up on a transient
                # network blip; the daily cron has a wall-clock cap above us.
                while True:
                    try:
                        conn = _reopen()
                        break
                    except Exception as reopen_err:
                        print(f"    [{sid}] reconnect failed "
                              f"({str(reopen_err)[:120]}) — "
                              f"sleeping 30s and retrying",
                              file=sys.stderr, flush=True)
                        time.sleep(30)
                try:
                    committed = _db_count(conn, sid)
                except Exception as inner:
                    print(f"    [{sid}] post-reconnect count failed: "
                          f"{inner} — retrying chunk", file=sys.stderr,
                          flush=True)
                    continue
                # If delete_pending was true at the time of the drop, the
                # transaction that would have run the DELETE rolled back;
                # the DB still has the pre-load rows. We need a fresh DELETE
                # next attempt and we should NOT count pre-load rows as
                # "committed chunk-0".
                if delete_pending:
                    # Pre-load rows linger — retry chunk 0 with delete_pending
                    # still set. inserted_so_far is still 0; don't advance.
                    print(f"    [{sid}] chunk-0 dropped before DELETE+INSERT "
                          f"committed (db still at pre={committed:,}) — "
                          f"retrying with fresh DELETE", flush=True)
                    continue
                expected_after_chunk = inserted_so_far + len(chunk)
                if committed >= expected_after_chunk:
                    print(f"    [{sid}] chunk WAS committed before drop "
                          f"(db={committed:,} >= expected={expected_after_chunk:,}) — "
                          f"advancing", flush=True)
                    inserted_so_far = committed
                    i += len(chunk)
                elif committed > inserted_so_far:
                    delta = committed - inserted_so_far
                    print(f"    [{sid}] partial chunk landed: "
                          f"{delta:,} rows committed; skipping ahead",
                          flush=True)
                    inserted_so_far = committed
                    i += delta
                else:
                    print(f"    [{sid}] chunk rolled back "
                          f"(db={committed:,} == inserted_so_far={inserted_so_far:,}) — "
                          f"retrying", flush=True)
                    # i stays — retry the same chunk.

    post = _db_count(conn, sid)
    return {"sid": sid, "pre": pre, "deleted": deleted,
            "inserted": n, "post": post,
            "elapsed_s": round(time.time() - t0, 1),
            "conn": conn}  # caller takes ownership of (possibly new) conn


def load_to_db(target_label: str, kw: dict, source_paths: dict, *,
               skip_if_loaded: bool = False) -> list[dict]:
    """Per-source DELETE+INSERT for every CSV in source_paths except sources
    flagged KEEP_EXISTING.

    skip_if_loaded: before DELETE, count rows for this source_id; if it
    already matches the CSV row count we just skip — useful for resuming
    a partial RDS run."""
    is_remote = "rds" in kw["host"]
    chunk = RDS_CHUNK_ROWS if is_remote else 0
    print(f"\n=== loading to {target_label} ({'chunked' if chunk else 'single-batch'}) ===")
    print(f"connecting to {kw['host']} ...")
    t0 = time.time()
    conn = psycopg2.connect(**kw)
    conn.autocommit = False
    results: list[dict] = []
    try:
        for sid, path in source_paths.items():
            if sid in KEEP_EXISTING:
                print(f"  [{sid}] SKIP — keeping existing data")
                results.append({"sid": sid, "skipped": True,
                                 "reason": "KEEP_EXISTING"})
                continue
            if not os.path.exists(path):
                print(f"  [{sid}] SKIP — CSV missing at {path}")
                results.append({"sid": sid, "skipped": True,
                                 "reason": "no csv"})
                continue
            if skip_if_loaded:
                # csv.reader handles embedded newlines inside quoted fields
                # (which our `address` / `details` fields routinely contain).
                # Naive `sum(1 for _ in open(...))` over-counted by ~5x and
                # caused every skip check to false-negative.
                with open(path, encoding="utf-8", newline="") as cf:
                    csv_n = sum(1 for _ in csv.reader(cf)) - 1
                cur_n = _db_count(conn, sid)
                print(f"  [{sid}] skip-check: db={cur_n:,} csv={csv_n:,}",
                      flush=True)
                if cur_n == csv_n and cur_n > 0:
                    print(f"  [{sid}] SKIP — already loaded "
                          f"(db={cur_n:,} == csv={csv_n:,})", flush=True)
                    results.append({"sid": sid, "skipped": True,
                                     "reason": "already_loaded",
                                     "post": cur_n})
                    continue
                if cur_n > csv_n:
                    # Stale duplicates from an earlier interrupted run.
                    # Don't trust them — fall through and let the DELETE+INSERT
                    # rebuild the source from the CSV's authoritative count.
                    print(f"  [{sid}] DB has more rows than CSV "
                          f"(db={cur_n:,} > csv={csv_n:,}) — re-loading clean",
                          flush=True)
            print(f"  [{sid}] loading {path} ...", flush=True)
            if is_remote:
                r = _copy_load_source(conn, sid, path, reconnect_kw=kw)
            else:
                r = load_one_source(conn, sid, path,
                                    chunk_rows=chunk,
                                    reconnect_kw=None)
            # Replace conn in case the loader reopened it.
            if "conn" in r:
                conn = r.pop("conn")
            results.append(r)
            print(f"    pre={r.get('pre','?'):>7,} → deleted={r.get('deleted','?'):>7,}"
                  f" → inserted={r.get('inserted','?'):>7,} → post={r.get('post','?'):>7,}"
                  f"  ({r.get('elapsed_s','?')}s)", flush=True)
    finally:
        try:
            conn.close()
        except Exception:
            pass
    print(f"=== {target_label} load complete in {time.time()-t0:.1f}s ===")
    return results


# ---------------------------------------------------------------------------
# Verification

def verify(target_label: str, kw: dict) -> None:
    print(f"\n=== verify {target_label} ===")
    conn = psycopg2.connect(**kw)
    try:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT source_id, COUNT(*)
                FROM watchlist_records
                WHERE source_id IN %s
                GROUP BY source_id
                ORDER BY COUNT(*) DESC;
            """, (tuple(sid for _, (sid, _) in SOURCE_MAP.items()),))
            rows = cur.fetchall()
    finally:
        conn.close()
    counts = {sid: n for sid, n in rows}
    for src_label, (sid, _) in SOURCE_MAP.items():
        n = counts.get(sid, 0)
        tag = "  (KEEP — pre-existing)" if sid in KEEP_EXISTING else ""
        print(f"  {sid:40s} {n:>9,}{tag}")


# ---------------------------------------------------------------------------
# CLI

def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                  formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--all", action="store_true",
                    help="transform + register + load both DBs")
    ap.add_argument("--transform", action="store_true",
                    help="stream JSON, write per-source CSVs (no DB)")
    ap.add_argument("--register", action="store_true",
                    help="register new sources in sources.json")
    ap.add_argument("--load", choices=("local", "rds", "both", "none"),
                    default="none")
    ap.add_argument("--verify", action="store_true",
                    help="print row counts after loading")
    ap.add_argument("--input", default=INPUT_JSON)
    ap.add_argument("--skip-if-loaded", action="store_true",
                    help="for each source, skip DELETE+INSERT if DB count "
                         "already equals CSV count (resume after TCP drop)")
    args = ap.parse_args()

    if args.all:
        args.transform = True
        args.register = True
        args.load = "both"
        args.verify = True

    if not (args.transform or args.register or args.load != "none"
            or args.verify):
        ap.error("specify --all, --transform, --register, --load, or --verify")

    source_paths: dict = {}
    if args.transform:
        print(f"=== transform from {args.input} ===")
        if not os.path.exists(args.input):
            print(f"FATAL: {args.input} not found", file=sys.stderr)
            return 2
        result = stream_transform(args.input)
        source_paths = result["paths"]
    else:
        # Rebuild paths map without re-running transform.
        for src_label, (sid, _) in SOURCE_MAP.items():
            if sid in KEEP_EXISTING:
                source_paths[sid] = os.path.join(DATA_DIR, f"gunjan_{sid}.csv")
            else:
                source_paths[sid] = os.path.join(DATA_DIR, f"{sid}.csv")

    if args.register:
        print("\n=== register new sources in sources.json ===")
        register_new_sources()

    if args.load in ("local", "both"):
        load_to_db("LOCAL", DB_LOCAL, source_paths,
                   skip_if_loaded=args.skip_if_loaded)
    if args.load in ("rds", "both"):
        load_to_db("RDS", DB_RDS, source_paths,
                   skip_if_loaded=args.skip_if_loaded)

    if args.verify:
        verify("LOCAL", DB_LOCAL)
        if args.load in ("rds", "both"):
            verify("RDS", DB_RDS)

    return 0


if __name__ == "__main__":
    sys.exit(main())
