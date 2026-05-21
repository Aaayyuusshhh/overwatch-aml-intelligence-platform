#!/usr/bin/env bash
# ----------------------------------------------------------------------------
# daily_pipeline.sh — knowledge-graph refresh + Slack health summary.
#
# This is the *post-scrape* daily pipeline. It assumes:
#   1. run_all.sh has already scraped + combined + loaded (cron at 06:00)
#   2. monitor_sources.py has snapshotted today's source health (cron at 07:00)
#
# It then refreshes the knowledge graph (entity_groups + entity_links + risk
# scoring) and posts a Slack summary. Schedule at 07:30 to avoid racing the
# 06:00 run_all.sh load.
#
# Manual run:  ./scripts/daily_pipeline.sh
# Cron:        30 7 * * * /home/aayush/risk-pipeline/scripts/daily_pipeline.sh
# ----------------------------------------------------------------------------

set -uo pipefail

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$PROJECT_ROOT"

mkdir -p logs reports
LOG="$PROJECT_ROOT/logs/daily_pipeline_$(date +%Y%m%d).log"
exec > >(tee -a "$LOG") 2>&1

log() { printf '[%s] %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$*"; }

if [ ! -x "venv/bin/python" ]; then
    log "FATAL: venv/bin/python missing"
    exit 1
fi
# shellcheck disable=SC1091
source venv/bin/activate

# Load .env if present (sets PG_HOST/PG_USER/PG_PASSWORD/PG_DB).
if [ -f .env ]; then
    set -a; source .env; set +a
fi

PG_HOST="${PG_HOST:-localhost}"
PG_USER="${PG_USER:-aayush}"
PG_DB="${PG_DB:-risk_pipeline}"
PG_PASSWORD="${PG_PASSWORD:-aayush123}"

# Mirror into psycopg2's standard env vars so Python scripts pick them up.
export PGHOST="$PG_HOST" PGUSER="$PG_USER" PGDATABASE="$PG_DB" PGPASSWORD="$PG_PASSWORD"

pq() { PGPASSWORD="$PG_PASSWORD" psql -h "$PG_HOST" -U "$PG_USER" -d "$PG_DB" -tAc "$1"; }

log "=== daily_pipeline.sh start ==="

# --------------------------------------------------------------------- Step 1
log "Step 1/5: source-health snapshot (idempotent; skips if already taken today)..."
TODAY_SNAPSHOTS=$(pq "SELECT COUNT(*) FROM source_health WHERE snapshot_date=CURRENT_DATE;" 2>/dev/null || echo 0)
if [ "${TODAY_SNAPSHOTS:-0}" -gt 0 ]; then
    log "  already have $TODAY_SNAPSHOTS rows for today — skipping snapshot"
else
    venv/bin/python scripts/monitor_sources.py --snapshot-only 2>&1 | tail -3
fi

# --------------------------------------------------------------------- Step 2
log "Step 2/5: health report (no Slack alert unless BROKEN/ANOMALY)..."
venv/bin/python scripts/monitor_sources.py --report-only 2>&1 | tail -3

# --------------------------------------------------------------------- Step 3
# Knowledge graph refresh. --reset is safe; rebuilds in ~3 min.
log "Step 3/5: knowledge graph rebuild (exact only — fuzzy is slow, run manually)..."
venv/bin/python scripts/knowledge_graph.py --build-exact --reset 2>&1 | tail -8

log "Step 4/5: knowledge graph risk-scoring..."
venv/bin/python scripts/knowledge_graph.py --risk-score 2>&1 | tail -5

# --------------------------------------------------------------------- Step 5
log "Step 5/5: Slack summary..."
TOTAL=$(pq    "SELECT COUNT(*) FROM watchlist_records;")
SOURCES=$(pq  "SELECT COUNT(DISTINCT source_id) FROM watchlist_records WHERE source_id IS NOT NULL AND source_id<>'';")
GROUPS=$(pq   "SELECT COUNT(*) FROM entity_groups;")
HIGH=$(pq     "SELECT COUNT(*) FROM entity_groups WHERE risk_level='HIGH';")
MED=$(pq      "SELECT COUNT(*) FROM entity_groups WHERE risk_level='MEDIUM';")
LINKS=$(pq    "SELECT COUNT(*) FROM entity_links;")
BROKEN=$(pq   "SELECT COUNT(*) FROM source_health WHERE snapshot_date=CURRENT_DATE AND status IN ('BROKEN','ANOMALY');")

# Slack webhook discovery: settings.local.json → env. Don't fail on missing.
WEBHOOK=""
if [ -f .claude/settings.local.json ]; then
    WEBHOOK=$(grep -oE 'https://hooks\.slack\.com/services/[A-Za-z0-9/_-]+' .claude/settings.local.json | head -1)
fi
[ -z "$WEBHOOK" ] && WEBHOOK="${SLACK_WEBHOOK_URL:-}"

if [ -n "$WEBHOOK" ]; then
    STATUS_EMOJI="✅"
    [ "${BROKEN:-0}" -gt 0 ] && STATUS_EMOJI="⚠️"
    BODY=$(cat <<EOF
{"text":"${STATUS_EMOJI} *Daily AML Pipeline Complete*\n• Records: ${TOTAL}\n• Sources: ${SOURCES}\n• Entity Groups: ${GROUPS}\n• Entity Links: ${LINKS}\n• HIGH Risk: ${HIGH} | MEDIUM: ${MED}\n• BROKEN/ANOMALY today: ${BROKEN}\n_$(date '+%Y-%m-%d %H:%M %Z')_"}
EOF
)
    if curl -s -X POST "$WEBHOOK" -H 'Content-type: application/json' -d "$BODY" \
         | grep -q "^ok$"; then
        log "  Slack: ok"
    else
        log "  Slack: POST failed"
    fi
else
    log "  Slack: no webhook configured — skipping"
fi

log "=== daily_pipeline.sh complete ==="
