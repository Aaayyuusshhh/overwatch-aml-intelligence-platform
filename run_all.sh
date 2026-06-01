#!/usr/bin/env bash
# Risk Pipeline daily runner.
#
# Workflow:
#   1. Snapshot DB row count BEFORE the run.
#   2. Activate the project venv and load .env.
#   3. main.py — scrapes every active source, combines master CSV,
#      loads into Postgres, posts Slack daily summary + email.
#   4. scripts/validate_production.py — data-quality gate that runs
#      after the load, writes a CSV report under reports/, and exits
#      non-zero on any CRITICAL finding.
#   5. Snapshot post-run row count.
#   6. Append a one-line summary to logs/run_YYYY-MM-DD.log so it's
#      grep-friendly from cron mails. The Slack daily summary itself
#      is already sent from inside main.py.
#
# Usage:
#   ./run_all.sh                 # full run
#   ./run_all.sh --dry-run       # skip main.py; only snapshot + validate
#
# Cron line (activate via scripts/activate_cron.sh):
#   0 6 * * * /home/aayush/risk-pipeline/run_all.sh \
#               >> /home/aayush/risk-pipeline/logs/cron.log 2>&1

set -uo pipefail

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$PROJECT_ROOT"

DRY_RUN=0
for arg in "$@"; do
    [ "$arg" = "--dry-run" ] && DRY_RUN=1
done

if [ ! -x "venv/bin/python" ]; then
    echo "run_all.sh: venv/bin/python not found at $PROJECT_ROOT/venv" >&2
    exit 1
fi

mkdir -p logs reports
TODAY="$(date +%Y-%m-%d)"
RUN_LOG="$PROJECT_ROOT/logs/run_${TODAY}.log"

log() {
    printf '[%s] %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$*" | tee -a "$RUN_LOG"
}

# .env carries Slack webhook + DB password.
if [ -f "$PROJECT_ROOT/.env" ]; then
    # shellcheck disable=SC1091
    set -a; source "$PROJECT_ROOT/.env"; set +a
fi
# shellcheck disable=SC1091
source venv/bin/activate

PG_USER="${PG_USER:-aayush}"
PG_DB="${PG_DB:-risk_pipeline}"
PG_PASSWORD="${PG_PASSWORD:-aayush123}"
PG_HOST="${PG_HOST:-localhost}"

count_rows() {
    PGPASSWORD="$PG_PASSWORD" psql -U "$PG_USER" -d "$PG_DB" -h "$PG_HOST" \
        -tA -c "SELECT count(*) FROM watchlist_records;" 2>/dev/null || echo "0"
}

log "=== run_all.sh start (dry_run=$DRY_RUN) ==="
PRE_COUNT=$(count_rows)
log "pre-run row count: $PRE_COUNT"

# Backup data/ CSVs before any scraping. The healer's rollback path
# (DATA_ZEROED alert) restores from this directory when a fresh scrape
# zeros out an otherwise-populated source.
log "backing up data/ CSVs ..."
mkdir -p data/backup
BAK_NEW=0
for csv in data/*.csv; do
    [ -f "$csv" ] || continue
    [ -s "$csv" ] || continue
    fname=$(basename "$csv")
    bak="data/backup/$fname"
    if [ ! -f "$bak" ] || [ "$csv" -nt "$bak" ]; then
        cp -p "$csv" "$bak"
        BAK_NEW=$((BAK_NEW + 1))
    fi
done
log "backup: refreshed $BAK_NEW files ($(ls data/backup/*.csv 2>/dev/null | wc -l) total in data/backup/)"

# Per-source pre-scrape counts. compare_counts.py reads this after the run
# to compute deltas the daily report can show (e.g. "OpenSanctions PEPs: +85").
log "saving pre-scrape per-source counts ..."
./venv/bin/python -c "
import json, psycopg2, os, sys
try:
    conn = psycopg2.connect(host='${PG_HOST}', user='${PG_USER}',
                             password='${PG_PASSWORD}', dbname='${PG_DB}')
    cur = conn.cursor()
    cur.execute(\"SELECT source_id, COUNT(*) FROM watchlist_records WHERE source_id IS NOT NULL AND source_id <> '' GROUP BY source_id\")
    counts = dict(cur.fetchall())
    json.dump(counts, open('logs/pre_scrape_counts.json', 'w'))
    print(f'pre-scrape: saved {len(counts)} source counts')
    conn.close()
except Exception as e:
    print(f'pre-scrape: WARN failed to snapshot counts: {e}', file=sys.stderr)
" >> "$RUN_LOG" 2>&1

MAIN_EXIT=0
if [ "$DRY_RUN" = "0" ]; then
    # ---------------------------------------------------------------------
    # Standalone scrapers. None of these go through main.py's sources.json
    # dispatch table — they write CSVs directly into data/, which combine.py
    # (inside main.py) then merges into master_watchlist.csv. They must run
    # BEFORE main.py so the fresh CSVs are present at combine time.
    #
    # Each is wrapped in `timeout` + `|| log WARN ...` so one failing scraper
    # never aborts the pipeline. Slow scrapers (MCA PDFs, etc.) are NOT in
    # the daily cron — see scripts/run_mca_weekly.sh.
    # ---------------------------------------------------------------------

    # OpenSanctions: ~200MB download + transform, 5-10 min. The transform
    # produces opensanctions_{debarment,crime,peps}.csv in data/.
    log "refreshing OpenSanctions ..."
    timeout 900 ./venv/bin/python scripts/download_opensanctions.py >> "$RUN_LOG" 2>&1
    OS_DL_EXIT=$?
    log "opensanctions download exit=$OS_DL_EXIT"
    if [ "$OS_DL_EXIT" -eq 0 ]; then
        timeout 300 ./venv/bin/python scripts/transform_opensanctions.py >> "$RUN_LOG" 2>&1
        log "opensanctions transform exit=$?"
    else
        log "WARN: skipping OpenSanctions transform (download failed/timed out)"
    fi

    # FATF black/grey lists: ~25 rows, instant.
    log "refreshing FATF lists ..."
    timeout 60 ./venv/bin/python scripts/create_fatf_lists.py >> "$RUN_LOG" 2>&1 \
        || log "WARN: FATF list refresh failed"

    # Europe scrapers (CSSF Luxembourg, CONSOB Italy, FI Sweden).
    log "running Europe scrapers ..."
    timeout 300 ./venv/bin/python scrapers/europe_scrapers.py >> "$RUN_LOG" 2>&1 \
        || log "WARN: europe scrapers timeout/error (exit=$?)"

    # Latin America scrapers (Brazil COAF, Argentina CNV).
    log "running Latin America scrapers ..."
    timeout 900 ./venv/bin/python scrapers/latam_scrapers.py >> "$RUN_LOG" 2>&1 \
        || log "WARN: latam scrapers timeout/error (exit=$?)"

    # Friday standalone scrapers (US/AU/NZ).
    log "running Friday US/AU/NZ scrapers ..."
    timeout 600 ./venv/bin/python scrapers/friday_us_au_nz_scrapers.py >> "$RUN_LOG" 2>&1
    FRIDAY_EXIT=$?
    log "friday scrapers exit=$FRIDAY_EXIT"

    # ---------------------------------------------------------------------
    # main.py: dispatches html/pdf/js/restricted/config sources from
    # sources.json, then runs combine.py + load_to_db.py.
    # ---------------------------------------------------------------------
    log "running main.py ..."
    ./venv/bin/python main.py >> "$RUN_LOG" 2>&1
    MAIN_EXIT=$?
    log "main.py exit=$MAIN_EXIT"
else
    log "DRY-RUN: skipping scrapers and main.py"
fi

# Source monitor v2 — replaces the old smart_change_detector.
# Checks every URL-bearing source from the outside (HTTP, content hash,
# layout fingerprint, staleness) so a silently-failed scraper still surfaces
# in the daily report. Writes logs/source_monitor_v2.json that the daily
# report + /api/pipeline/status both read. Capped at 10 minutes — the full
# HTTP+content sweep across 942 sources runs in ~5 min on a clean line.
log "running source monitor v2 ..."
timeout 600 ./venv/bin/python scripts/source_monitor_v2.py --all --slack \
    >> "$RUN_LOG" 2>&1
MONITOR_EXIT=$?
log "source monitor v2 exit=$MONITOR_EXIT"

# Auto-healer: reads logs/source_monitor_v2.json and acts on its alerts.
# Re-scrapes content-changed/stale/recovered sources, rolls back zeroed
# data from data/backup/, tries Playwright fallback on newly-blocked HTML
# sources, and re-downloads stale OpenSanctions / FATF feeds. Hard-skips
# MCA (weekly cron) and any source still in 24h cooldown from yesterday.
# Capped at 30 min wall-clock and 10 actions per run.
log "running auto-healer ..."
timeout 1800 ./venv/bin/python scripts/auto_healer.py --all --slack \
    >> "$RUN_LOG" 2>&1
HEALER_EXIT=$?
log "auto-healer exit=$HEALER_EXIT"

log "running validator ..."
./venv/bin/python scripts/validate_production.py \
    --report "reports/validation_${TODAY}.csv" \
    >> "$RUN_LOG" 2>&1
VALIDATOR_EXIT=$?
log "validator exit=$VALIDATOR_EXIT"

POST_COUNT=$(count_rows)
DELTA=$(( POST_COUNT - PRE_COUNT ))
log "post-run row count: $POST_COUNT (delta=$DELTA)"

# Per-source diff: reads logs/pre_scrape_counts.json + queries current
# counts, writes logs/post_scrape_diff.json. The daily report reads that
# JSON to surface "+85 new OpenSanctions PEPs" instead of a vague total.
log "comparing per-source counts ..."
./venv/bin/python scripts/compare_counts.py >> "$RUN_LOG" 2>&1
COMPARE_EXIT=$?
log "compare_counts exit=$COMPARE_EXIT"

# Daily report (HTML email via SES + rich Slack). Tolerant: failure here
# does not fail the whole pipeline; the report itself reports its own status.
log "sending daily report ..."
./venv/bin/python scripts/send_daily_report.py >> "$RUN_LOG" 2>&1
REPORT_EXIT=$?
log "daily report exit=$REPORT_EXIT"

# Final one-line summary — easy to grep / forward from cron mail.
SUMMARY="rows ${PRE_COUNT}->${POST_COUNT} (delta ${DELTA}) | main.py exit=${MAIN_EXIT} | monitor exit=${MONITOR_EXIT} | healer exit=${HEALER_EXIT:-0} | validator exit=${VALIDATOR_EXIT} | report exit=${REPORT_EXIT}"
log "SUMMARY: $SUMMARY"
log "=== run_all.sh end ==="

# Non-zero exit if either step flagged a problem so cron's MAILTO surfaces
# it. (Validator exits 1 only on CRITICAL findings.)
if [ "$MAIN_EXIT" -ne 0 ] || [ "$VALIDATOR_EXIT" -ne 0 ]; then
    exit 1
fi
exit 0
