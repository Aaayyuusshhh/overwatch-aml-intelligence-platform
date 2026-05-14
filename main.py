"""
Risk Pipeline orchestrator.

Reads sources.json, runs change detection (per source's
'change_detection' flag), dispatches each active source by type to
the matching handler, and invokes the validator on every successful
scrape's CSV. Per-source exceptions are caught at every layer so one
failure cannot halt the run.

Push alerts via utils.alerter on:
  - layout change detected (change_detector returns 'changed')
  - scraper failure (handler status == 'failure')
  - validator soft-failure (validator subprocess exit != 0)
  - end-of-run summary

Logging via utils.logger writes to logs/run_YYYY-MM-DD_HH-MM.log AND
stdout per ARCHITECTURE.md §3.5 / PRD §6.10.
"""

import json
import os
import subprocess
import sys

from handlers import html_handler, pdf_handler, js_handler, restricted_handler, config_handler
from utils.alerter import send_alert
from utils.notifier import (
    send_daily_summary as slack_daily_summary,
    send_layout_change_alert as slack_layout_change,
    send_error_alert as slack_error,
    send_email_report,
)
from utils.change_detector import check_for_change
from utils.logger import get_log_file_path, log_event

PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))
SOURCES_PATH = os.path.join(PROJECT_ROOT, "sources.json")
VALIDATOR_PATH = os.path.join(PROJECT_ROOT, "scripts", "validate_cbi.py")
LAST_RUN_STATS_PATH = os.path.join(PROJECT_ROOT, "logs", "last_run_stats.json")

HANDLER_BY_TYPE = {
    "html": html_handler.handle,
    "pdf": pdf_handler.handle,
    "js": js_handler.handle,
    "restricted": restricted_handler.handle,
    "config": config_handler.handle,
}


def load_sources(path):
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)["sources"]


def _read_db_totals():
    """Return (total_rows, distinct_agencies). Best-effort: returns (0, 0)
    if psycopg2 isn't installed or the DB is unreachable. The pipeline
    has already loaded data by the time we call this."""
    try:
        import psycopg2
        conn = psycopg2.connect(
            host=os.environ.get("PG_HOST", "localhost"),
            user=os.environ.get("PG_USER", "aayush"),
            password=os.environ.get("PG_PASSWORD", "aayush123"),
            dbname=os.environ.get("PG_DB", "risk_pipeline"),
        )
        cur = conn.cursor()
        cur.execute("SELECT COUNT(*) FROM watchlist_records")
        total = cur.fetchone()[0]
        cur.execute("SELECT COUNT(DISTINCT source_agency) FROM watchlist_records")
        agencies = cur.fetchone()[0]
        conn.close()
        return int(total), int(agencies)
    except Exception as e:
        log_event(None, "db_totals_err",
                  f"{type(e).__name__}: {e}", level="warning")
        return 0, 0


def _read_tracker_breakdown():
    """Read project_status.xlsx and return (status->count, completed_count).
    Returns ({}, 0) if the tracker isn't available yet."""
    try:
        import openpyxl
        path = os.path.join(PROJECT_ROOT, "project_status.xlsx")
        wb = openpyxl.load_workbook(path, read_only=True)
        ws = wb.active
        rows = ws.iter_rows(values_only=True)
        header = list(next(rows))
        i_status = header.index("status")
        breakdown = {}
        for row in rows:
            s = row[i_status] or "unknown"
            breakdown[s] = breakdown.get(s, 0) + 1
        return breakdown, int(breakdown.get("completed", 0))
    except Exception as e:
        log_event(None, "tracker_read_err",
                  f"{type(e).__name__}: {e}", level="warning")
        return {}, 0


def _load_last_run_stats():
    if not os.path.exists(LAST_RUN_STATS_PATH):
        return {}
    try:
        with open(LAST_RUN_STATS_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def _save_last_run_stats(stats):
    try:
        os.makedirs(os.path.dirname(LAST_RUN_STATS_PATH), exist_ok=True)
        with open(LAST_RUN_STATS_PATH, "w", encoding="utf-8") as f:
            json.dump(stats, f, indent=2)
    except Exception as e:
        log_event(None, "last_run_stats_save_err",
                  f"{type(e).__name__}: {e}", level="warning")


def run_change_detection(source):
    """Returns 'first_run' / 'unchanged' / 'changed' / 'skipped'.
    'skipped' covers 'change_detection: false' in config and
    type=='pdf' (handled inside check_for_change). When the source
    config provides 'change_detection_selector', the detector hashes
    only that subtree (defeats false positives from rotating CSRF
    meta tags etc.)."""
    sid = source["id"]
    if not source.get("change_detection", False):
        return "skipped"
    return check_for_change(
        sid,
        source.get("url"),
        source.get("type"),
        source.get("change_detection_selector"),
    )


def validate_csv(source_id, csv_path):
    """Invoke scripts/validate_cbi.py against csv_path. Returns subprocess
    exit code or None on error."""
    if not csv_path or not os.path.exists(csv_path):
        log_event(source_id, "validate_skip", f"no CSV at {csv_path!r}")
        return None
    try:
        result = subprocess.run(
            [sys.executable, VALIDATOR_PATH, csv_path],
            capture_output=True, text=True, timeout=60,
        )
        action = "validate_ok" if result.returncode == 0 else "validate_fail"
        log_event(source_id, action, f"exit={result.returncode}")
        return result.returncode
    except subprocess.TimeoutExpired:
        log_event(source_id, "validate_timeout", "")
        return None
    except Exception as e:
        log_event(source_id, "validate_error", f"{type(e).__name__}: {e}")
        return None


def run_source(source, layout_changed_ids):
    """Dispatch one source: change detection, handler, validator, alerts."""
    sid = source.get("id", "<no_id>")
    src_type = source.get("type", "unknown")

    # Change detection (per-source flag).
    cd_result = run_change_detection(source)
    log_event(sid, f"change_{cd_result}")
    if cd_result == "changed":
        layout_changed_ids.append(sid)
        send_alert(f"Layout change detected for {sid}", severity="warning")
        # Slack: immediate layout-change alert. Hashes aren't surfaced
        # by the current detector return value, so we pass placeholders;
        # the alert names the source, agency, and URL which is what an
        # on-call engineer needs to act.
        try:
            slack_layout_change(
                source_name=source.get("list_name", sid),
                agency=source.get("agency", "?"),
                url=source.get("url") or "(no url)",
                old_hash="prev",
                new_hash="curr",
            )
        except Exception as e:
            log_event(sid, "slack_layout_change_err",
                      f"{type(e).__name__}: {e}", level="warning")

    handler = HANDLER_BY_TYPE.get(src_type)
    if handler is None:
        log_event(sid, "dispatch_error", f"no handler for type={src_type!r}")
        send_alert(f"No handler for {sid} type={src_type!r}", severity="error")
        return {"status": "failure", "record_count": 0,
                "runtime_seconds": 0.0,
                "error": f"no handler for type={src_type!r}",
                "csv_path": None}

    log_event(sid, "dispatch", f"-> {handler.__module__} (type={src_type})")
    try:
        result = handler(source)
    except Exception as e:
        log_event(sid, "handler_exception", f"{type(e).__name__}: {e}",
                  level="error")
        send_alert(f"Handler exception on {sid}: {type(e).__name__}: {e}",
                   severity="error")
        try:
            slack_error(
                source_name=source.get("list_name", sid),
                agency=source.get("agency", "?"),
                error_msg=f"{type(e).__name__}: {e}",
            )
        except Exception as se:
            log_event(sid, "slack_error_err",
                      f"{type(se).__name__}: {se}", level="warning")
        return {"status": "failure", "record_count": 0,
                "runtime_seconds": 0.0,
                "error": f"{type(e).__name__}: {e}",
                "csv_path": None}

    log_event(sid, f"handler_{result['status']}",
              f"records={result['record_count']} "
              f"runtime={result['runtime_seconds']}s "
              f"fetch_tier={result.get('fetch_tier', '-')!s} "
              f"error={result['error']!r}")

    if result["status"] == "failure":
        send_alert(f"Scraper failure on {sid}: {result['error']}",
                   severity="error")
    elif result["status"] == "skipped":
        # PDF-missing or restricted; no alert (reason already logged).
        pass
    elif result["status"] == "success":
        rc = validate_csv(sid, result["csv_path"])
        # Soft failure: validator non-zero exit. Scrape stays 'success';
        # an alert is sent. (Validator currently always exits 0, so this
        # branch is dormant until validate_cbi.py is updated to surface
        # findings via exit code.)
        if rc is not None and rc != 0:
            send_alert(
                f"Validator reported issues on {sid} (exit={rc})",
                severity="warning",
            )
    return result


def run():
    log_event(None, "run_start", f"reading {SOURCES_PATH}")
    try:
        sources = load_sources(SOURCES_PATH)
    except Exception as e:
        log_event(None, "fatal", f"could not load sources.json: {e}",
                  level="error")
        send_alert(f"Pipeline aborted: cannot load sources.json: {e}",
                   severity="error")
        sys.exit(1)
    log_event(None, "loaded", f"{len(sources)} entries")

    summary = {"success": 0, "failure": 0, "skipped": 0, "pending": 0,
               "total_records": 0}
    layout_changed_ids = []
    layout_changes_detail = []  # for daily Slack summary
    failures_detail = []        # for daily Slack summary

    for source in sources:
        sid = source.get("id", "<no_id>")
        status = source.get("status", "unknown")
        if status == "pending_recon":
            summary["pending"] += 1
            log_event(sid, "skip_pending", f"agency={source.get('agency')!r}")
            continue
        # Per task brief: don't notify Slack about skipped/duplicate sources.
        if status != "active":
            log_event(sid, "skip_other", f"status={status!r}")
            continue

        layout_before = list(layout_changed_ids)
        result = run_source(source, layout_changed_ids)
        if len(layout_changed_ids) > len(layout_before):
            layout_changes_detail.append({
                "source": source.get("list_name", sid),
                "agency": source.get("agency", "?"),
                "detail": "hash changed",
            })

        if result["status"] == "success":
            summary["success"] += 1
            summary["total_records"] += result["record_count"]
        elif result["status"] == "failure":
            summary["failure"] += 1
            failures_detail.append({
                "source": f"{source.get('list_name', sid)}",
                "agency": source.get("agency", "?"),
                "reason": (result.get("error") or "")[:160],
            })
        else:
            summary["skipped"] += 1

    # Post-scrape aggregation: master CSV + DB load + tracker spreadsheet.
    # Per ARCHITECTURE.md §4.1 (steps 5-7). Order matters: combine must
    # finish before load_to_db reads master_watchlist.csv.
    run_post_step("combine", os.path.join(PROJECT_ROOT, "scripts", "combine.py"))
    run_post_step("load_db", os.path.join(PROJECT_ROOT, "scripts", "load_to_db.py"))
    run_post_step("tracker", os.path.join(PROJECT_ROOT, "scripts", "generate_tracker.py"))

    summary_msg = (
        f"success={summary['success']} failure={summary['failure']} "
        f"skipped={summary['skipped']} pending={summary['pending']} "
        f"total_records={summary['total_records']} "
        f"layout_changes={len(layout_changed_ids)}"
    )
    log_event(None, "run_summary", summary_msg)
    send_alert("Pipeline run complete: " + summary_msg
               + (f"  changed={layout_changed_ids}" if layout_changed_ids else ""),
               severity="info")

    # Slack daily summary -- post-aggregation so DB + tracker reflect this run.
    breakdown, completed_count = _read_tracker_breakdown()
    db_total, db_agencies = _read_db_totals()
    last = _load_last_run_stats()
    last_total = int(last.get("records_total", 0))
    records_new = db_total - last_total if last_total else 0

    from datetime import datetime as _dt, timezone as _tz, timedelta as _td
    ist_now = _dt.now(_tz(_td(hours=5, minutes=30)))
    stats = {
        "date": ist_now.strftime("%-d %b %Y"),
        "total_sources": len(sources),
        "completed": completed_count,
        "records_total": db_total,
        "records_new": records_new,
        "agencies": db_agencies,
        "successful_runs": summary["success"],
        "failed_runs": summary["failure"],
        "skipped_runs": summary["skipped"],
        "layout_changes": layout_changes_detail,
        "failures": failures_detail,
        "tracker_breakdown": breakdown,
    }
    try:
        slack_daily_summary(stats)
    except Exception as e:
        log_event(None, "slack_daily_summary_err",
                  f"{type(e).__name__}: {e}", level="warning")
    try:
        send_email_report(stats)
    except Exception as e:
        log_event(None, "email_report_err",
                  f"{type(e).__name__}: {e}", level="warning")
    _save_last_run_stats({
        "records_total": db_total,
        "completed": completed_count,
        "agencies": db_agencies,
        "ts": ist_now.isoformat(),
    })

    log_event(None, "run_end", f"log_file={get_log_file_path()}")


def run_post_step(label, script_path):
    """Subprocess-invoke a post-run script and log result."""
    if not os.path.exists(script_path):
        log_event(None, f"{label}_skip", f"missing {script_path}")
        return
    try:
        result = subprocess.run(
            [sys.executable, script_path],
            capture_output=True, text=True, timeout=120,
        )
        action = f"{label}_ok" if result.returncode == 0 else f"{label}_fail"
        log_event(None, action, f"exit={result.returncode}")
        if result.stdout:
            for line in result.stdout.rstrip().splitlines():
                log_event(None, label, line)
        if result.returncode != 0:
            send_alert(f"{label} step failed (exit={result.returncode})",
                       severity="warning")
    except subprocess.TimeoutExpired:
        log_event(None, f"{label}_timeout", "")
        send_alert(f"{label} step timed out", severity="warning")
    except Exception as e:
        log_event(None, f"{label}_error", f"{type(e).__name__}: {e}",
                  level="error")
        send_alert(f"{label} step exception: {e}", severity="error")


if __name__ == "__main__":
    run()

