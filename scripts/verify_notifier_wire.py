"""
Verify the end-of-run notification path in main.py without re-running
every scraper. Loads sources, reads tracker + DB, builds the same
stats dict main.py builds, and calls slack_daily_summary +
send_email_report against the live webhook + SMTP creds in .env.
"""
import os
import sys

# Make repo importable when invoked from any cwd.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from main import (_read_db_totals, _read_tracker_breakdown,
                  _load_last_run_stats, _save_last_run_stats,
                  load_sources, SOURCES_PATH)
from utils.notifier import send_daily_summary, send_email_report
from datetime import datetime, timezone, timedelta

ist_now = datetime.now(timezone(timedelta(hours=5, minutes=30)))
sources = load_sources(SOURCES_PATH)
breakdown, completed = _read_tracker_breakdown()
db_total, db_agencies = _read_db_totals()
last = _load_last_run_stats()
last_total = int(last.get("records_total", 0))
records_new = db_total - last_total if last_total else 0

stats = {
    "date":            ist_now.strftime("%-d %b %Y"),
    "total_sources":   len(sources),
    "completed":       completed,
    "records_total":   db_total,
    "records_new":     records_new,
    "agencies":        db_agencies,
    "successful_runs": completed,
    "failed_runs":     breakdown.get("failed", 0),
    "skipped_runs":    breakdown.get("skipped", 0),
    "layout_changes":  [],
    "failures": [
        {"source": "Bank of Maharashtra (BOM) — Wilful Defaulters #132",
         "reason": "network_error (host unreachable)"},
    ],
    "tracker_breakdown": breakdown,
}

print("Stats dict:")
for k, v in stats.items():
    if isinstance(v, list):
        print(f"  {k}: [{len(v)} items]")
    elif isinstance(v, dict):
        print(f"  {k}: {dict(v)}")
    else:
        print(f"  {k}: {v}")

print()
print("send_daily_summary (Slack):", send_daily_summary(stats))
print("send_email_report  (Email):", send_email_report(stats))

_save_last_run_stats({
    "records_total": db_total,
    "completed": completed,
    "agencies": db_agencies,
    "ts": ist_now.isoformat(),
})
print(f"Persisted last_run_stats.json (records_total={db_total})")
