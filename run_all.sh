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

MAIN_EXIT=0
if [ "$DRY_RUN" = "0" ]; then
    # Friday standalone scrapers (US/AU/NZ). These don't go through the
    # sources.json dispatch table — they write CSVs directly into data/,
    # which combine.py (inside main.py) then merges into master_watchlist.csv.
    # Must run BEFORE main.py so the fresh CSVs are present at combine time.
    log "running Friday US/AU/NZ scrapers ..."
    ./venv/bin/python scrapers/friday_us_au_nz_scrapers.py >> "$RUN_LOG" 2>&1
    FRIDAY_EXIT=$?
    log "friday scrapers exit=$FRIDAY_EXIT"

    log "running main.py ..."
    ./venv/bin/python main.py >> "$RUN_LOG" 2>&1
    MAIN_EXIT=$?
    log "main.py exit=$MAIN_EXIT"
else
    log "DRY-RUN: skipping main.py"
fi

log "running smart change detector ..."
./venv/bin/python utils/smart_change_detector.py --all --slack \
    >> "$RUN_LOG" 2>&1
DETECTOR_EXIT=$?
log "change detector exit=$DETECTOR_EXIT"

log "running validator ..."
./venv/bin/python scripts/validate_production.py \
    --report "reports/validation_${TODAY}.csv" \
    >> "$RUN_LOG" 2>&1
VALIDATOR_EXIT=$?
log "validator exit=$VALIDATOR_EXIT"

POST_COUNT=$(count_rows)
DELTA=$(( POST_COUNT - PRE_COUNT ))
log "post-run row count: $POST_COUNT (delta=$DELTA)"

# Daily report (HTML email via SES + rich Slack). Tolerant: failure here
# does not fail the whole pipeline; the report itself reports its own status.
log "sending daily report ..."
./venv/bin/python scripts/send_daily_report.py >> "$RUN_LOG" 2>&1
REPORT_EXIT=$?
log "daily report exit=$REPORT_EXIT"

# Final one-line summary — easy to grep / forward from cron mail.
SUMMARY="rows ${PRE_COUNT}->${POST_COUNT} (delta ${DELTA}) | main.py exit=${MAIN_EXIT} | validator exit=${VALIDATOR_EXIT} | report exit=${REPORT_EXIT}"
log "SUMMARY: $SUMMARY"
log "=== run_all.sh end ==="

# Non-zero exit if either step flagged a problem so cron's MAILTO surfaces
# it. (Validator exits 1 only on CRITICAL findings.)
if [ "$MAIN_EXIT" -ne 0 ] || [ "$VALIDATOR_EXIT" -ne 0 ]; then
    exit 1
fi
exit 0
