#!/usr/bin/env bash
# ----------------------------------------------------------------------------
# run_mca_weekly.sh — weekly MCA PDF scrape (1000+ PDFs, 4-6 hours).
#
# MCA scrapers fetch and OCR PDFs from the Ministry of Corporate Affairs.
# They're far too slow for the daily 6 AM cron (the load alone takes hours),
# so we run them once a week on Sunday at 02:00 IST when the box is idle.
#
# Each source_id runs --resume so a re-run picks up where the previous run
# left off (resumes from existing CSV by skipping already-downloaded PDFs).
# --limit-pdfs 0 means "no per-source cap" and --max-pages 200 caps per-PDF
# OCR work so a single huge PDF can't stall the run.
#
# Output:   data/<source_id>.csv  (combined into master_watchlist by the
#           next daily run_all.sh via combine.py)
# Log:      logs/mca_weekly_YYYYMMDD.log
# Cron:     0 2 * * 0 /home/aayush/risk-pipeline/scripts/run_mca_weekly.sh
# ----------------------------------------------------------------------------

set -uo pipefail

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$PROJECT_ROOT"

mkdir -p logs
LOG="logs/mca_weekly_$(date +%Y%m%d).log"

log() { printf '[%s] %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$*" | tee -a "$LOG"; }

if [ ! -x "venv/bin/python" ]; then
    log "FATAL: venv/bin/python missing"
    exit 1
fi
# shellcheck disable=SC1091
source venv/bin/activate
if [ -f .env ]; then
    set -a; source .env; set +a
fi

SOURCES=(
    mca_disqualified_directors_164
    mca_companies_struck_off
    mca_public_notices_stk6
    mca_directors_struck_off_248
    mca_public_notices_stk5
    mca_llp_strike_off_rule37
    mca_notice_strike_off_stk7
    mca_rd_compounding_orders
)

log "=== mca_weekly start ($(date -Iseconds)) ==="
log "scraping ${#SOURCES[@]} MCA sources with --resume; per-source limit-pdfs=0, max-pages=200"

OVERALL_RC=0
for sid in "${SOURCES[@]}"; do
    log "--- $sid: START ---"
    # 4-hour ceiling per source; if a single source hangs we move on rather
    # than wedging the whole weekly run.
    timeout 14400 venv/bin/python -u scrapers/mca_rd_roc.py \
        --only "$sid" --limit-pdfs 0 --max-pages 200 --resume \
        >> "$LOG" 2>&1
    RC=$?
    log "--- $sid: DONE (exit=$RC) ---"
    [ "$RC" -ne 0 ] && OVERALL_RC=$RC
done

log "=== mca_weekly complete (overall_rc=$OVERALL_RC) ==="

# Trigger a one-shot load of the freshly-updated CSVs into the DB so the
# next daily run already sees the new data. Best-effort: do not fail the
# weekly script if the loader has trouble — it will retry tomorrow.
if [ -x "scripts/load_mca_pending.py" ] || [ -f "scripts/load_mca_pending.py" ]; then
    log "loading new MCA rows to local DB ..."
    timeout 3600 venv/bin/python scripts/load_mca_pending.py >> "$LOG" 2>&1 \
        || log "WARN: load_mca_pending failed (exit=$?) — will retry on daily run"
fi

# Post a Slack ping so the team knows the weekly run finished.
WEBHOOK="${SLACK_WEBHOOK_URL:-}"
if [ -z "$WEBHOOK" ] && [ -f .claude/settings.local.json ]; then
    WEBHOOK=$(grep -oE 'https://hooks\.slack\.com/services/[A-Za-z0-9/_-]+' \
        .claude/settings.local.json | head -1)
fi
if [ -n "$WEBHOOK" ]; then
    STATUS_ICON=":white_check_mark:"
    [ "$OVERALL_RC" -ne 0 ] && STATUS_ICON=":warning:"
    BODY=$(cat <<EOF
{"text":"${STATUS_ICON} MCA weekly scrape complete. overall_rc=${OVERALL_RC}. See logs/mca_weekly_$(date +%Y%m%d).log"}
EOF
)
    curl -sS -X POST "$WEBHOOK" -H 'Content-type: application/json' -d "$BODY" \
        >/dev/null 2>&1 || log "WARN: Slack ping failed"
fi

exit "$OVERALL_RC"
