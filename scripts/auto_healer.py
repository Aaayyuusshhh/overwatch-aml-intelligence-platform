#!/usr/bin/env python3
"""scripts/auto_healer.py — autonomous self-healing for the daily pipeline.

Reads logs/source_monitor_v2.json (produced by source_monitor_v2.py), classifies
each alert into a concrete action, executes the action, reloads any new data
into both local Postgres and the RDS replica, and writes a healing report
(logs/auto_healer_report.json) that the daily email/Slack and the
/api/pipeline/status endpoint consume.

The monitor DETECTS problems. The healer ACTS on them.

Alert -> action map
-------------------
  CONTENT_CHANGED   -> rescrape_content     (upstream updated, re-pull)
  LAYOUT_CHANGED    -> rescrape_layout      (test scraper still works)
  DATA_ZEROED       -> rollback_zeroed      (restore from data/backup/)
  STALE (>=7d)      -> rescrape_stale       (force refresh)
  HTTP recovery     -> rescrape_recovery    (was DOWN/BLOCKED, now OK)
  HTTP_BLOCKED new  -> playwright_fallback  (was OK, now blocked)
  BULK_STALE        -> redownload_bulk      (OpenSanctions / FATF)

Hard skips (the daily healer must NOT trigger these):
  * MCA sources (scrapers take hours; weekly cron only)
  * Sources without a URL or with status in {dead_url, url_not_found}
  * Sources whose last heal attempt failed within the last 24h (cooldown)

Guardrails
----------
  --max-actions 10         (default; bounds runaway healing on mass alerts)
  --total-timeout 1800     (overall wall-clock budget in seconds)
  Each individual scraper runs inside its own subprocess with its own timeout.

Output
------
  logs/auto_healer_report.json   today's actions + their outcomes
  logs/auto_healer_state.json    persistent state (cooldowns, recovery tracking)

CLI
---
  auto_healer.py --all                       full run on latest monitor JSON
  auto_healer.py --all --dry-run             classify but don't execute
  auto_healer.py --source <sid>              heal one source explicitly
  auto_healer.py --force-rescrape <sid> ...  re-scrape regardless of alerts
  auto_healer.py --content-changes-only      ignore stale/recovery/blocked
  auto_healer.py --stale-only                only refresh stale sources
  auto_healer.py --slack                     post a Slack summary at the end
  auto_healer.py --verbose                   chatty per-step logging

INTERNAL (used by subprocess isolation, don't call by hand):
  auto_healer.py --run-handler <sid>     execute one scraper, print JSON result
"""
from __future__ import annotations

import argparse
import csv
import datetime as dt
import importlib
import json
import os
import shutil
import subprocess
import sys
import time
import traceback
from typing import Optional

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LOG_DIR = os.path.join(PROJECT_ROOT, "logs")
DATA_DIR = os.path.join(PROJECT_ROOT, "data")
BACKUP_DIR = os.path.join(DATA_DIR, "backup")
SOURCES_PATH = os.path.join(PROJECT_ROOT, "sources.json")
MONITOR_PATH = os.path.join(LOG_DIR, "source_monitor_v2.json")
REPORT_PATH = os.path.join(LOG_DIR, "auto_healer_report.json")
STATE_PATH = os.path.join(LOG_DIR, "auto_healer_state.json")
ENV_PATH = os.path.join(PROJECT_ROOT, ".env")

VENV_PY = os.path.join(PROJECT_ROOT, "venv", "bin", "python")
if not os.path.exists(VENV_PY):
    VENV_PY = sys.executable

DEFAULT_SCRAPER_TIMEOUT = 300         # 5 min per scraper
BULK_TIMEOUT = 900                    # 15 min for OpenSanctions
COOLDOWN_HOURS = 24                   # don't retry the same source for 24h
MAX_ACTIONS_DEFAULT = 10
TOTAL_TIMEOUT_DEFAULT = 1800          # 30 min hard cap

BULK_PREFIXES = ("opensanctions_", "fatf_")
SKIP_PREFIXES = ("mca_", "icij_")     # MCA is weekly-only; ICIJ is a static dump

# Priority order — lower = handled first.
PRIORITY = {
    "rollback_zeroed":     0,
    "rescrape_content":    1,
    "rescrape_layout":     2,
    "rescrape_recovery":   3,
    "redownload_bulk":     4,
    "rescrape_stale":      5,
    "playwright_fallback": 6,
}

VERBOSE = False


# ---------------------------------------------------------------------------
# env + logging + IO

def load_env() -> dict:
    env = dict(os.environ)
    if os.path.exists(ENV_PATH):
        with open(ENV_PATH) as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, _, v = line.partition("=")
                env.setdefault(k.strip(), v.strip().strip('"').strip("'"))
    return env


ENV = load_env()


def log(msg: str, level: str = "INFO") -> None:
    if level == "DEBUG" and not VERBOSE:
        return
    ts = dt.datetime.now().strftime("%H:%M:%S")
    print(f"[{ts}] {level:<5} {msg}", flush=True)


def load_json(path: str, default):
    if not os.path.exists(path):
        return default
    try:
        with open(path) as f:
            return json.load(f)
    except Exception as e:
        log(f"could not read {path}: {e}", "WARN")
        return default


def save_json(path: str, data) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(data, f, indent=2, default=str)
    os.replace(tmp, path)


def count_csv_rows(path: Optional[str]) -> int:
    if not path or not os.path.exists(path):
        return 0
    try:
        with open(path, encoding="utf-8", errors="replace", newline="") as f:
            n = sum(1 for _ in csv.reader(f))
        return max(0, n - 1)
    except Exception:
        return 0


def load_sources() -> dict:
    """Return {source_id: source_dict}."""
    with open(SOURCES_PATH) as f:
        data = json.load(f)
    return {s.get("id"): s for s in data.get("sources", []) if s.get("id")}


# ---------------------------------------------------------------------------
# Alert classification

def classify_alert(alert: dict, prev_state: dict, src: Optional[dict]) -> Optional[dict]:
    """Convert one monitor alert into a healing action (or None to ignore)."""
    atype = alert.get("type")
    sid = alert.get("source_id")
    if not sid:
        return None

    # Hard skip dead/missing sources — we can't do anything about them.
    if src is not None:
        st = src.get("status")
        if st in ("dead_url", "url_not_found", "dead", "duplicate"):
            return None
    # Also skip MCA / ICIJ in the daily healer — too slow / static.
    if sid.startswith(SKIP_PREFIXES):
        return None

    if atype == "CONTENT_CHANGED":
        return {"source_id": sid, "action_type": "rescrape_content",
                "reason": alert.get("message", "content hash changed")}

    if atype == "LAYOUT_CHANGED":
        return {"source_id": sid, "action_type": "rescrape_layout",
                "reason": alert.get("message", "layout fingerprint drifted"),
                "severity": alert.get("severity")}

    if atype == "DATA_ZEROED":
        return {"source_id": sid, "action_type": "rollback_zeroed",
                "reason": alert.get("message", "data dropped to zero"),
                "pre_count": int(alert.get("pre", 0) or 0)}

    if atype == "STALE" and int(alert.get("age_days", 0) or 0) >= 7:
        # Bulk sources have their own freshness alert (BULK_STALE).
        if sid.startswith(BULK_PREFIXES):
            return None
        return {"source_id": sid, "action_type": "rescrape_stale",
                "reason": f"stale {alert.get('age_days')} days"}

    if atype == "BULK_STALE":
        return {"source_id": sid, "action_type": "redownload_bulk",
                "reason": alert.get("message", "bulk source is stale")}

    if atype == "HTTP_BLOCKED":
        prev = (prev_state.get(sid) or {}).get("last_http")
        if prev == "HTTP_OK":
            return {"source_id": sid, "action_type": "playwright_fallback",
                    "reason": "was OK, now blocked — trying Playwright"}
        return None  # Already blocked yesterday; don't keep retrying.

    if atype == "HTTP_OK":
        # Recovery: was DOWN/BLOCKED yesterday, healthy today.
        prev = (prev_state.get(sid) or {}).get("last_http")
        if prev in ("HTTP_DOWN", "HTTP_BLOCKED"):
            return {"source_id": sid, "action_type": "rescrape_recovery",
                    "reason": "source recovered from outage"}
        return None

    # Everything else (HTTP_DOWN, HTTP_REDIRECT, VERY_STALE, BELOW_MIN_RECORDS,
    # MISSING_FROM_SNAPSHOT, FATF_LIST_CHANGED) is informational — surface in
    # the report but no autonomous action.
    return None


def deduplicate_actions(actions: list[dict]) -> list[dict]:
    """One action per source — keep the highest-priority one."""
    by_sid: dict[str, dict] = {}
    for a in actions:
        sid = a["source_id"]
        prev = by_sid.get(sid)
        if prev is None or PRIORITY.get(a["action_type"], 99) < PRIORITY.get(prev["action_type"], 99):
            by_sid[sid] = a
    return list(by_sid.values())


def apply_cooldown(actions: list[dict], state: dict, now: dt.datetime) -> tuple[list[dict], list[dict]]:
    """Drop actions for sources that failed within COOLDOWN_HOURS. Returns
    (allowed, skipped_with_reason)."""
    allowed, skipped = [], []
    cutoff = now - dt.timedelta(hours=COOLDOWN_HOURS)
    for a in actions:
        sid = a["source_id"]
        entry = state.get(sid) or {}
        last_failed_iso = entry.get("last_failed")
        if last_failed_iso:
            try:
                t = dt.datetime.fromisoformat(
                    last_failed_iso.replace("Z", "").replace("+00:00", ""))
                if t.tzinfo is None:
                    t = t.replace(tzinfo=dt.timezone.utc)
                if t > cutoff:
                    a["status"] = "skipped_cooldown"
                    a["message"] = f"failed at {last_failed_iso} — cooldown {COOLDOWN_HOURS}h"
                    skipped.append(a)
                    continue
            except Exception:
                pass
        allowed.append(a)
    return allowed, skipped


# ---------------------------------------------------------------------------
# Scraper invocation (subprocess-isolated)

def run_handler_subprocess(sid: str, timeout: int) -> dict:
    """Invoke `auto_healer.py --run-handler <sid>` as a subprocess so a hung
    or crashing scraper can never take down the healer.

    The child process loads the source from sources.json, dispatches via the
    same HANDLER_BY_TYPE table main.py uses, and prints a one-line JSON result
    on its last stdout line. We parse that line and return its fields.
    """
    cmd = [VENV_PY, "-u",
           os.path.join("scripts", "auto_healer.py"),
           "--run-handler", sid]
    t0 = time.time()
    try:
        proc = subprocess.run(
            cmd, capture_output=True, text=True,
            timeout=timeout, cwd=PROJECT_ROOT,
        )
    except subprocess.TimeoutExpired:
        return {"status": "timeout",
                "message": f"scraper exceeded {timeout}s timeout",
                "elapsed_s": round(time.time() - t0, 1)}
    elapsed = round(time.time() - t0, 1)
    if proc.returncode != 0:
        stderr_tail = (proc.stderr or "")[-500:]
        parsed = _parse_last_json_line(proc.stdout)
        if parsed:
            parsed["elapsed_s"] = elapsed
            parsed.setdefault("stderr_tail", stderr_tail)
            return parsed
        return {"status": "subprocess_failed",
                "exit_code": proc.returncode,
                "stderr_tail": stderr_tail,
                "elapsed_s": elapsed}
    parsed = _parse_last_json_line(proc.stdout)
    if parsed is None:
        return {"status": "no_result_json",
                "stdout_tail": (proc.stdout or "")[-400:],
                "elapsed_s": elapsed}
    parsed["elapsed_s"] = elapsed
    return parsed


def _parse_last_json_line(text: str) -> Optional[dict]:
    if not text:
        return None
    for line in reversed(text.rstrip().splitlines()):
        line = line.strip()
        if line.startswith("{") and line.endswith("}"):
            try:
                obj = json.loads(line)
                if isinstance(obj, dict):
                    return obj
            except Exception:
                continue
    return None


def cmd_run_handler(sid: str) -> int:
    """Internal entrypoint: load the source, dispatch via the same table
    main.py uses, print a single-line JSON result. Stderr is fine for logs."""
    sources = load_sources()
    src = sources.get(sid)
    result: dict = {"source_id": sid}
    if not src:
        result.update({"status": "unknown_source",
                       "message": f"{sid} not in sources.json"})
        print(json.dumps(result), flush=True)
        return 1

    stype = src.get("type")
    sys.path.insert(0, PROJECT_ROOT)
    try:
        from handlers import (html_handler, pdf_handler, js_handler,
                              restricted_handler, config_handler)
    except Exception as e:
        result.update({"status": "import_error",
                       "message": f"{type(e).__name__}: {e}"})
        print(json.dumps(result), flush=True)
        return 1

    handler_table = {
        "html":       html_handler.handle,
        "pdf":        pdf_handler.handle,
        "js":         js_handler.handle,
        "restricted": restricted_handler.handle,
        "config":     config_handler.handle,
    }
    handler = handler_table.get(stype)
    if handler is not None:
        try:
            r = handler(src)
        except Exception as e:
            result.update({"status": "handler_exception",
                           "message": f"{type(e).__name__}: {str(e)[:300]}",
                           "trace": traceback.format_exc()[-600:]})
            print(json.dumps(result), flush=True)
            return 1
        if r.get("status") == "success":
            result.update({"status": "success",
                           "csv_path": r.get("csv_path"),
                           "post_rows": int(r.get("record_count") or 0),
                           "fetch_tier": r.get("fetch_tier")})
            print(json.dumps(result), flush=True)
            return 0
        result.update({"status": "handler_" + str(r.get("status", "failure")),
                       "message": (r.get("error") or "")[:300],
                       "csv_path": r.get("csv_path"),
                       "post_rows": int(r.get("record_count") or 0)})
        print(json.dumps(result), flush=True)
        return 1 if r.get("status") == "failure" else 0

    # No handler for this type — try to import the declared scraper module
    # directly. Supports both forms used in sources.json:
    #   "scraper": "cbi_red_notices.py"            -> scrapers.cbi_red_notices
    #   "scraper": "scrapers/mca_company_llp.py"   -> scrapers.mca_company_llp
    #   "scraper": "friday_us_au_nz_scrapers.scrape_apra_disqualified"
    #             -> scrapers.friday_us_au_nz_scrapers.scrape_apra_disqualified
    scraper_field = src.get("scraper")
    if not scraper_field:
        result.update({"status": "no_handler",
                       "message": f"no handler for type={stype!r} and no scraper field"})
        print(json.dumps(result), flush=True)
        return 1

    raw = scraper_field.strip()
    if raw.endswith(".py"):
        raw = raw[:-3]
    raw = raw.replace("/", ".")
    if not raw.startswith("scrapers."):
        raw = "scrapers." + raw
    try:
        # Try as a full module name first.
        try:
            mod = importlib.import_module(raw)
            target_callable = getattr(mod, "run", None)
            if target_callable is None:
                raise ModuleNotFoundError(f"{raw} has no run()")
        except ModuleNotFoundError:
            # Fall back to 'module.func' form
            parts = raw.split(".")
            base, func = ".".join(parts[:-1]), parts[-1]
            mod = importlib.import_module(base)
            if not hasattr(mod, func):
                result.update({"status": "scraper_not_found",
                               "message": f"{base}.{func} missing"})
                print(json.dumps(result), flush=True)
                return 1
            target_callable = getattr(mod, func)
        target_callable()
        csv_path = getattr(mod, "OUTPUT_FILE", None)
        if not csv_path or not os.path.exists(csv_path):
            guess = os.path.join(DATA_DIR, f"{sid}.csv")
            csv_path = guess if os.path.exists(guess) else csv_path
        result.update({"status": "success",
                       "csv_path": csv_path,
                       "post_rows": count_csv_rows(csv_path),
                       "fetch_tier": "custom_module"})
        print(json.dumps(result), flush=True)
        return 0
    except Exception as e:
        result.update({"status": "scraper_exception",
                       "message": f"{type(e).__name__}: {str(e)[:300]}",
                       "trace": traceback.format_exc()[-600:]})
        print(json.dumps(result), flush=True)
        return 1


# ---------------------------------------------------------------------------
# Bulk re-download (OpenSanctions, FATF)

def run_bulk_redownload(sid: str) -> dict:
    """Re-run the standalone bulk-download scripts. These produce CSVs in
    data/ that the next combine.py run picks up — but the healer also reloads
    them into the DB itself so the change is visible immediately."""
    if sid.startswith("opensanctions_"):
        steps = [
            ("download", [VENV_PY, os.path.join("scripts", "download_opensanctions.py")], BULK_TIMEOUT),
            ("transform", [VENV_PY, os.path.join("scripts", "transform_opensanctions.py")], 300),
        ]
    elif sid.startswith("fatf_"):
        steps = [("fatf", [VENV_PY, os.path.join("scripts", "create_fatf_lists.py")], 120)]
    else:
        return {"status": "no_bulk_handler"}

    for label, cmd, tmo in steps:
        log(f"  [{sid}] bulk step: {label}", "DEBUG")
        try:
            proc = subprocess.run(cmd, capture_output=True, text=True,
                                  timeout=tmo, cwd=PROJECT_ROOT)
        except subprocess.TimeoutExpired:
            return {"status": "bulk_timeout",
                    "message": f"{label} exceeded {tmo}s"}
        if proc.returncode != 0:
            return {"status": "bulk_failed",
                    "message": f"{label} exit={proc.returncode}",
                    "stderr_tail": (proc.stderr or "")[-300:]}
    csv_map = {
        "opensanctions_peps":       "opensanctions_peps.csv",
        "opensanctions_crime":      "opensanctions_crime.csv",
        "opensanctions_debarment":  "opensanctions_debarment.csv",
        "fatf_lists":               "fatf_lists.csv",
        "fatf_blacklist":           "fatf_lists.csv",
        "fatf_greylist":            "fatf_lists.csv",
    }
    fname = csv_map.get(sid, f"{sid}.csv")
    csv_path = os.path.join(DATA_DIR, fname)
    return {"status": "success",
            "csv_path": csv_path if os.path.exists(csv_path) else None,
            "post_rows": count_csv_rows(csv_path),
            "fetch_tier": "bulk"}


# ---------------------------------------------------------------------------
# Rollback (data zeroed)

def run_rollback(sid: str, action: dict, src: dict) -> dict:
    """If a backup CSV exists in data/backup/, restore it. Otherwise flag
    for human."""
    candidates = [f"{sid}.csv"]
    scr = src.get("scraper")
    if scr:
        scr = os.path.basename(scr).replace(".py", "")
        candidates.append(f"{scr}.csv")
    for cand in candidates:
        backup_path = os.path.join(BACKUP_DIR, cand)
        current_path = os.path.join(DATA_DIR, cand)
        if os.path.exists(backup_path) and count_csv_rows(backup_path) > 0:
            try:
                shutil.copy2(backup_path, current_path)
            except Exception as e:
                return {"status": "rollback_copy_failed",
                        "message": f"{type(e).__name__}: {e}"}
            rows = count_csv_rows(current_path)
            return {"status": "rolled_back",
                    "csv_path": current_path,
                    "post_rows": rows,
                    "message": f"restored {rows} rows from backup/{cand}"}
    return {"status": "no_backup",
            "message": f"data zeroed and no backup CSV found in {BACKUP_DIR}"}


# ---------------------------------------------------------------------------
# Playwright fallback

def run_playwright_fallback(sid: str, src: dict) -> dict:
    url = src.get("url")
    if not url:
        return {"status": "no_url"}
    try:
        from playwright.sync_api import sync_playwright  # type: ignore
    except Exception:
        return {"status": "playwright_not_installed",
                "message": "venv/bin/pip install playwright && playwright install chromium"}
    selector = src.get("change_detection_selector")
    rows_extracted: list[list[str]] = []
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            try:
                page = browser.new_page()
                page.set_extra_http_headers({"Accept-Language": "en-US,en;q=0.9"})
                page.goto(url, wait_until="networkidle", timeout=30000)
                for table in page.query_selector_all("table"):
                    for tr in table.query_selector_all("tr"):
                        cells = [td.inner_text().strip()
                                 for td in tr.query_selector_all("td")]
                        if any(cells):
                            rows_extracted.append(cells)
                if not rows_extracted and selector:
                    for el in page.query_selector_all(selector):
                        text = el.inner_text().strip()
                        if text:
                            rows_extracted.append([text])
            finally:
                browser.close()
    except Exception as e:
        return {"status": "playwright_failed",
                "message": f"{type(e).__name__}: {str(e)[:300]}"}

    if not rows_extracted:
        return {"status": "playwright_empty",
                "message": "page loaded but no table data extracted"}

    out_path = os.path.join(DATA_DIR, f"{sid}_playwright.csv")
    try:
        max_cols = max(len(r) for r in rows_extracted)
        with open(out_path, "w", encoding="utf-8", newline="") as f:
            w = csv.writer(f)
            w.writerow([f"col_{i+1}" for i in range(max_cols)])
            for r in rows_extracted:
                w.writerow(r + [""] * (max_cols - len(r)))
    except Exception as e:
        return {"status": "playwright_write_failed",
                "message": f"{type(e).__name__}: {e}"}
    return {"status": "playwright_success",
            "csv_path": out_path,
            "post_rows": len(rows_extracted),
            "message": f"Playwright extracted {len(rows_extracted)} rows to "
                       f"{os.path.basename(out_path)} (review before adoption)"}


# ---------------------------------------------------------------------------
# DB reload (targeted DELETE + execute_values into local + RDS)

SCHEMA_COLS = [
    "source_id", "source_agency", "source_list", "case_unit", "name",
    "father_name", "date_of_birth", "gender", "address", "reward_amount",
    "details", "has_document", "document_url", "detail_page_url",
    "interpol_notice_id", "link_kind", "scraped_at", "enrichment_status",
]


def _db_targets() -> list[tuple[str, dict]]:
    """Connection kwargs for both DB targets. RDS is included only when its
    host differs from PG_HOST (so EC2-side runs don't double-write)."""
    local = dict(
        host=ENV.get("PG_HOST", "localhost"),
        user=ENV.get("PG_USER", "aayush"),
        password=ENV.get("PG_PASSWORD", "aayush123"),
        dbname=ENV.get("PG_DB", "risk_pipeline"),
        connect_timeout=15,
    )
    targets = [("local", local)]
    rds_host = ENV.get("RDS_HOST", "overwatch-aml.cnsgg0i0cxa7.ap-south-1.rds.amazonaws.com")
    rds_pw = ENV.get("RDS_PASSWORD", "Aaayyuusshhh")
    if rds_host and rds_host != local["host"]:
        rds = dict(host=rds_host, user=ENV.get("RDS_USER", "aayush"),
                   password=rds_pw, dbname=ENV.get("RDS_DB", "risk_pipeline"),
                   connect_timeout=30)
        targets.append(("rds", rds))
    return targets


def reload_source_to_db(sid: str, csv_path: str, src: dict) -> dict:
    """Delete watchlist_records WHERE source_id=sid, then insert rows from CSV.

    Returns {local: {pre, deleted, post, ok}, rds: {...}} (rds only if remote
    target is configured and reachable).
    """
    out: dict = {}
    if not csv_path or not os.path.exists(csv_path):
        return {"error": "csv_missing", "csv_path": csv_path}

    try:
        import psycopg2
        import psycopg2.extras
    except Exception as e:
        return {"error": f"psycopg2_unavailable: {e}"}

    agency = (src.get("agency") or "").strip()
    list_name = (src.get("list_name") or "").strip()

    rows: list[tuple] = []
    try:
        with open(csv_path, encoding="utf-8", errors="replace", newline="") as f:
            reader = csv.DictReader(f)
            for r in reader:
                # When the CSV bundles multiple sources (e.g. fatf_lists.csv),
                # filter to just rows for this sid by agency+list.
                if (r.get("source_agency") and agency and
                        r.get("source_agency") != agency):
                    continue
                if (r.get("source_list") and list_name and
                        r.get("source_list") != list_name):
                    continue
                row_sid = (r.get("source_id") or sid).strip() or sid
                row_ag = (r.get("source_agency") or agency).strip() or agency
                row_ls = (r.get("source_list") or list_name).strip() or list_name
                rows.append(tuple(
                    (row_sid if c == "source_id" else
                     row_ag if c == "source_agency" else
                     row_ls if c == "source_list" else
                     (r.get(c) or ""))
                    for c in SCHEMA_COLS
                ))
    except Exception as e:
        return {"error": f"csv_read: {e}"}

    if not rows:
        return {"error": "no_rows_matched", "csv_path": csv_path,
                "agency": agency, "list_name": list_name}

    for label, kw in _db_targets():
        target_out: dict = {"rows_in_csv": len(rows)}
        try:
            conn = psycopg2.connect(**kw)
        except Exception as e:
            target_out["ok"] = False
            target_out["error"] = f"connect: {type(e).__name__}: {str(e)[:200]}"
            out[label] = target_out
            continue
        try:
            conn.autocommit = False
            with conn.cursor() as cur:
                cur.execute("SELECT COUNT(*) FROM watchlist_records WHERE source_id = %s;",
                            (sid,))
                target_out["pre"] = int(cur.fetchone()[0])
                cur.execute("DELETE FROM watchlist_records WHERE source_id = %s;",
                            (sid,))
                target_out["deleted"] = cur.rowcount
                psycopg2.extras.execute_values(
                    cur,
                    f"INSERT INTO watchlist_records ({','.join(SCHEMA_COLS)}) VALUES %s",
                    rows, page_size=5000,
                )
                cur.execute("SELECT COUNT(*) FROM watchlist_records WHERE source_id = %s;",
                            (sid,))
                target_out["post"] = int(cur.fetchone()[0])
            conn.commit()
            target_out["ok"] = True
        except Exception as e:
            try:
                conn.rollback()
            except Exception:
                pass
            target_out["ok"] = False
            target_out["error"] = f"{type(e).__name__}: {str(e)[:200]}"
        finally:
            conn.close()
        out[label] = target_out
    return out


# ---------------------------------------------------------------------------
# Action execution

def execute_action(action: dict, sources: dict) -> dict:
    sid = action["source_id"]
    atype = action["action_type"]
    src = sources.get(sid) or {}
    out = dict(action)

    if atype == "rollback_zeroed":
        out.update(run_rollback(sid, action, src))
        return out

    if atype == "redownload_bulk":
        out.update(run_bulk_redownload(sid))
        return out

    if atype == "playwright_fallback":
        out.update(run_playwright_fallback(sid, src))
        return out

    timeout = DEFAULT_SCRAPER_TIMEOUT
    if sid.startswith(BULK_PREFIXES):
        timeout = BULK_TIMEOUT
    pre_csv_path = os.path.join(DATA_DIR, f"{sid}.csv")
    pre_rows = count_csv_rows(pre_csv_path)
    res = run_handler_subprocess(sid, timeout)
    out.update(res)
    out.setdefault("pre_rows", pre_rows)
    if out.get("status") == "success":
        post = int(out.get("post_rows") or 0)
        out["delta"] = post - pre_rows
        if post == 0 and pre_rows > 0:
            out["status"] = "empty_result"
            out["message"] = f"scraper succeeded but produced 0 rows (was {pre_rows})"
    return out


# ---------------------------------------------------------------------------
# State management

def update_state(monitor: dict, prev_state: dict, results: list[dict],
                 now: dt.datetime) -> dict:
    """Build the next state file. Records every source's last HTTP status (so
    next run can detect recovery) and the last-failure timestamp per source."""
    new_state: dict = {}

    # Carry forward last-failed timestamps from prev_state.
    for sid, entry in prev_state.items():
        if entry.get("last_failed"):
            new_state[sid] = {"last_failed": entry["last_failed"],
                              "last_failed_status": entry.get("last_failed_status")}

    # The monitor's alerts only contain problem sources. We don't get a healthy
    # roster directly. So we update last_http only for sources that DID appear
    # in an http-typed alert; everything else carries forward from prev_state.
    for sid, entry in prev_state.items():
        if entry.get("last_http"):
            new_state.setdefault(sid, {})
            new_state[sid]["last_http"] = entry["last_http"]

    for alert in monitor.get("alerts", []):
        sid = alert.get("source_id")
        atype = alert.get("type")
        if not sid or atype not in ("HTTP_OK", "HTTP_DOWN", "HTTP_BLOCKED",
                                    "HTTP_REDIRECT", "HTTP_ERROR"):
            continue
        entry = new_state.setdefault(sid, {})
        entry["last_http"] = atype

    # Sources that previously had HTTP problems but aren't in today's alerts
    # are healthy now — mark HTTP_OK so next run's classifier sees recovery.
    flagged = {a["source_id"] for a in monitor.get("alerts", [])
               if a.get("source_id") and a.get("type") in
               ("HTTP_DOWN", "HTTP_BLOCKED", "HTTP_ERROR", "HTTP_REDIRECT")}
    for sid, entry in list(prev_state.items()):
        if (entry.get("last_http") in ("HTTP_DOWN", "HTTP_BLOCKED",
                                       "HTTP_ERROR", "HTTP_REDIRECT")
                and sid not in flagged):
            new_state.setdefault(sid, {})["last_http"] = "HTTP_OK"

    # Record this run's outcomes.
    for r in results:
        sid = r.get("source_id")
        if not sid:
            continue
        entry = new_state.setdefault(sid, {})
        status = r.get("status", "")
        if status in ("success", "rolled_back", "playwright_success"):
            entry["last_healed"] = now.isoformat(timespec="seconds")
            entry.pop("last_failed", None)
            entry.pop("last_failed_status", None)
        elif status in ("dry_run", "skipped_cooldown"):
            pass
        else:
            entry["last_failed"] = now.isoformat(timespec="seconds")
            entry["last_failed_status"] = status
    return new_state


# ---------------------------------------------------------------------------
# Reporting

def build_report(monitor: dict, actions: list[dict], results: list[dict],
                 args, now: dt.datetime, started: float) -> dict:
    healed = [r for r in results if r.get("status") in ("success", "playwright_success")]
    rolled_back = [r for r in results if r.get("status") == "rolled_back"]
    needs_human_statuses = {
        "no_scraper", "no_handler", "no_backup", "playwright_not_installed",
        "playwright_empty", "playwright_failed", "empty_result",
        "no_run_in_module", "scraper_not_found", "unknown_source", "no_url",
    }
    needs_human = [r for r in results if r.get("status") in needs_human_statuses]
    skipped = [r for r in results if r.get("status") in
               ("dry_run", "skipped_cooldown")]
    handled = {"success", "rolled_back", "playwright_success",
               "dry_run", "skipped_cooldown"} | needs_human_statuses
    failed = [r for r in results if r.get("status") not in handled]

    total_new_rows = sum(r.get("delta", 0) for r in healed if r.get("delta", 0) > 0)
    rows_restored = sum(r.get("post_rows", 0) for r in rolled_back)
    rows_reloaded = sum(
        ((r.get("db_reload") or {}).get("local") or {}).get("post", 0)
        for r in healed + rolled_back
    )

    return {
        "timestamp": now.isoformat(timespec="seconds"),
        "monitor_timestamp": monitor.get("timestamp"),
        "args": {k: v for k, v in vars(args).items() if not k.startswith("_")},
        "total_alerts": len(monitor.get("alerts", [])),
        "total_actions_classified": len(actions),
        "total_actions_executed": len(results),
        "healed": len(healed),
        "rolled_back": len(rolled_back),
        "needs_human": len(needs_human),
        "failed": len(failed),
        "skipped": len(skipped),
        "total_new_rows": total_new_rows,
        "rows_restored": rows_restored,
        "rows_reloaded_to_db": rows_reloaded,
        "elapsed_seconds": round(time.time() - started, 1),
        "summary": {
            "auto_healed": [
                {"source_id": r["source_id"],
                 "action": r.get("action_type"),
                 "post_rows": r.get("post_rows", 0),
                 "delta": r.get("delta", 0),
                 "reason": r.get("reason", "")}
                for r in healed
            ],
            "rolled_back": [
                {"source_id": r["source_id"],
                 "rows_restored": r.get("post_rows", 0),
                 "reason": r.get("reason", "")}
                for r in rolled_back
            ],
            "needs_attention": [
                {"source_id": r["source_id"],
                 "status": r.get("status"),
                 "message": (r.get("message") or "")[:300]}
                for r in needs_human
            ],
            "failures": [
                {"source_id": r["source_id"],
                 "status": r.get("status"),
                 "message": (r.get("message") or "")[:300]}
                for r in failed
            ],
        },
        "results": results,
    }


def print_summary(report: dict) -> None:
    print("=" * 70)
    print(f"Auto-Healer — {report['timestamp']}")
    print("=" * 70)
    print(f"  alerts classified   : {report['total_actions_classified']}")
    print(f"  actions executed    : {report['total_actions_executed']}")
    print(f"  healed              : {report['healed']}")
    print(f"  rolled back         : {report['rolled_back']}")
    print(f"  needs human         : {report['needs_human']}")
    print(f"  failed              : {report['failed']}")
    print(f"  skipped (dry/cool)  : {report['skipped']}")
    print(f"  new rows from heals : {report['total_new_rows']:,}")
    print(f"  rows restored       : {report['rows_restored']:,}")
    print(f"  rows reloaded to DB : {report['rows_reloaded_to_db']:,}")
    print(f"  elapsed             : {report['elapsed_seconds']}s")
    if report["summary"]["auto_healed"]:
        print("\n  Auto-healed sources:")
        for h in report["summary"]["auto_healed"][:20]:
            print(f"    + {h['source_id']:40s} {(h['action'] or ''):22s} "
                  f"post={h['post_rows']:>6} delta={h['delta']:+}")
    if report["summary"]["rolled_back"]:
        print("\n  Rolled back:")
        for h in report["summary"]["rolled_back"]:
            print(f"    * {h['source_id']:40s} restored {h['rows_restored']} rows")
    if report["summary"]["needs_attention"]:
        print("\n  Needs human attention:")
        for h in report["summary"]["needs_attention"][:10]:
            print(f"    ! {h['source_id']:40s} {h['status']:25s} {h['message'][:60]}")
    if report["summary"]["failures"]:
        print("\n  Failures:")
        for h in report["summary"]["failures"][:10]:
            print(f"    x {h['source_id']:40s} {h['status']:25s} {h['message'][:60]}")


def post_slack(report: dict) -> None:
    webhook = ENV.get("SLACK_WEBHOOK_URL", "")
    if not webhook:
        log("no SLACK_WEBHOOK_URL — skipping Slack post", "WARN")
        return
    if (report["healed"] == 0 and report["rolled_back"] == 0
            and report["needs_human"] == 0 and report["failed"] == 0):
        log("nothing to report — skipping Slack", "INFO")
        return

    s = report["summary"]
    lines = [
        f"Auto-healer ran on monitor report {report.get('monitor_timestamp','-')}",
        f":wrench: *Auto-healed:* {report['healed']} sources "
        f"(+{report['total_new_rows']:,} new rows)",
    ]
    if report["rolled_back"]:
        lines.append(f":arrows_counterclockwise: *Rolled back:* "
                     f"{report['rolled_back']} sources "
                     f"({report['rows_restored']:,} rows restored from backup)")
    if report["needs_human"]:
        lines.append(f":warning: *Needs human attention:* {report['needs_human']} sources")
    if report["failed"]:
        lines.append(f":x: *Scraper failures:* {report['failed']} sources")

    blocks = [
        {"type": "header",
         "text": {"type": "plain_text", "text": "🔧 Overwatch AML — Auto-Healer"}},
        {"type": "section",
         "text": {"type": "mrkdwn", "text": "\n".join(lines)}},
    ]

    if s["auto_healed"][:6]:
        body = "\n".join(
            (f"• `{h['source_id']}` {h['action']} → +{h['delta']:,} rows"
             if h.get("delta", 0) > 0 else
             f"• `{h['source_id']}` {h['action']} (post={h['post_rows']:,})")
            for h in s["auto_healed"][:6]
        )
        blocks.append({"type": "divider"})
        blocks.append({"type": "section",
                       "text": {"type": "mrkdwn",
                                "text": "*Auto-healed sources:*\n" + body}})

    if s["needs_attention"][:6]:
        body = "\n".join(
            f"• `{h['source_id']}` _{h['status']}_: {h['message'][:120]}"
            for h in s["needs_attention"][:6]
        )
        blocks.append({"type": "divider"})
        blocks.append({"type": "section",
                       "text": {"type": "mrkdwn",
                                "text": "*Needs human attention:*\n" + body}})

    payload = {"blocks": blocks, "text": "Auto-healer report"}
    try:
        import urllib.request
        req = urllib.request.Request(
            webhook,
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=15) as resp:
            log(f"Slack post: http={resp.status}", "INFO")
    except Exception as e:
        log(f"Slack post failed: {e}", "WARN")


# ---------------------------------------------------------------------------
# Orchestrator

def collect_actions(args, monitor: dict, prev_state: dict, sources: dict) -> list[dict]:
    """Build the action list."""
    if args.force_rescrape:
        return [{"source_id": sid, "action_type": "rescrape_content",
                 "reason": "forced via --force-rescrape"}
                for sid in args.force_rescrape if sid in sources]

    actions: list[dict] = []
    for alert in monitor.get("alerts", []):
        a = classify_alert(alert, prev_state,
                           sources.get(alert.get("source_id") or ""))
        if a:
            actions.append(a)
    actions = deduplicate_actions(actions)

    if args.content_changes_only:
        actions = [a for a in actions if a["action_type"] in
                   ("rescrape_content", "rescrape_layout", "rollback_zeroed")]
    if args.stale_only:
        actions = [a for a in actions if a["action_type"] == "rescrape_stale"]
    if args.source:
        actions = [a for a in actions if a["source_id"] == args.source]
        if not actions and args.source in sources:
            actions = [{"source_id": args.source,
                        "action_type": "rescrape_content",
                        "reason": "explicit --source request"}]

    actions.sort(key=lambda a: PRIORITY.get(a["action_type"], 99))
    return actions


def run(args) -> int:
    started = time.time()
    now = dt.datetime.now(dt.timezone.utc)
    sources = load_sources()
    monitor = load_json(MONITOR_PATH, {})
    if not monitor and not args.force_rescrape:
        log(f"no monitor report at {MONITOR_PATH} — run source_monitor_v2.py first",
            "WARN")
        return 2

    prev_state = load_json(STATE_PATH, {})
    actions = collect_actions(args, monitor, prev_state, sources)
    log(f"classified {len(actions)} actions from "
        f"{len(monitor.get('alerts', []))} alerts")

    actions, cooldown_skipped = apply_cooldown(actions, prev_state, now)
    if cooldown_skipped:
        log(f"  skipped {len(cooldown_skipped)} sources still in cooldown")

    if len(actions) > args.max_actions:
        log(f"  capping at --max-actions={args.max_actions} "
            f"({len(actions) - args.max_actions} deferred)", "WARN")
        actions = actions[: args.max_actions]

    results: list[dict] = list(cooldown_skipped)

    if args.dry_run:
        for a in actions:
            log(f"  [DRY] {a['action_type']:22s} {a['source_id']:40s} "
                f"{a.get('reason','')[:60]}")
            results.append({**a, "status": "dry_run",
                            "message": "dry-run; not executed"})
    else:
        deadline = started + args.total_timeout
        for a in actions:
            if time.time() > deadline:
                log(f"  total-timeout {args.total_timeout}s reached — "
                    f"deferring remaining actions", "WARN")
                break
            log(f"  → {a['action_type']:22s} {a['source_id']:40s} "
                f"{a.get('reason','')[:60]}")
            try:
                r = execute_action(a, sources)
            except Exception as e:
                r = {**a, "status": "executor_exception",
                     "message": f"{type(e).__name__}: {str(e)[:200]}"}
            # On success, reload to DB. Skip for Playwright (schema unknown).
            if r.get("status") in ("success", "rolled_back"):
                csv_path = r.get("csv_path")
                if csv_path:
                    try:
                        r["db_reload"] = reload_source_to_db(
                            a["source_id"], csv_path,
                            sources.get(a["source_id"]) or {})
                    except Exception as e:
                        r["db_reload"] = {"error":
                            f"{type(e).__name__}: {str(e)[:200]}"}
            log(f"    = {r.get('status')} "
                f"post_rows={r.get('post_rows','-')} "
                f"delta={r.get('delta','-')} "
                f"elapsed={r.get('elapsed_s','-')}s")
            results.append(r)

    new_state = update_state(monitor, prev_state, results, now)
    save_json(STATE_PATH, new_state)

    report = build_report(monitor, actions, results, args, now, started)
    save_json(REPORT_PATH, report)
    log(f"wrote {REPORT_PATH}")

    print_summary(report)

    if args.slack and not args.dry_run:
        post_slack(report)

    return 0


# ---------------------------------------------------------------------------
# CLI

def main() -> int:
    global VERBOSE
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--all", action="store_true",
                    help="heal every source flagged by the monitor")
    ap.add_argument("--source", default=None,
                    help="heal one specific source_id (even if no alert)")
    ap.add_argument("--force-rescrape", nargs="*", default=[],
                    help="force re-scrape these source_ids regardless of alerts")
    ap.add_argument("--content-changes-only", action="store_true",
                    help="ignore stale/recovery/blocked alerts")
    ap.add_argument("--stale-only", action="store_true",
                    help="only refresh STALE alerts")
    ap.add_argument("--dry-run", action="store_true",
                    help="classify but don't execute")
    ap.add_argument("--slack", action="store_true",
                    help="post a Slack summary at the end")
    ap.add_argument("--verbose", action="store_true",
                    help="chatty per-step logging")
    ap.add_argument("--max-actions", type=int, default=MAX_ACTIONS_DEFAULT,
                    help=f"cap actions per run (default {MAX_ACTIONS_DEFAULT})")
    ap.add_argument("--total-timeout", type=int, default=TOTAL_TIMEOUT_DEFAULT,
                    help=f"overall wall-clock budget (default {TOTAL_TIMEOUT_DEFAULT}s)")
    ap.add_argument("--run-handler", default=None, help=argparse.SUPPRESS)
    args = ap.parse_args()
    VERBOSE = args.verbose

    if args.run_handler:
        return cmd_run_handler(args.run_handler)

    if not (args.all or args.source or args.force_rescrape):
        ap.error("specify one of --all, --source, or --force-rescrape")
    return run(args)


if __name__ == "__main__":
    sys.exit(main())
