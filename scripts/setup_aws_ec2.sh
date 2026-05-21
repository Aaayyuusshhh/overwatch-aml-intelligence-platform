#!/usr/bin/env bash
# -----------------------------------------------------------------------------
# setup_aws_ec2.sh — bootstrap the Overwatch AML pipeline on a fresh EC2 host.
#
# Run AS root (sudo) on a freshly launched Ubuntu 24.04 EC2 instance.
# It will:
#   1. Install system deps (Python 3.12, PostgreSQL client, Chromium for Playwright).
#   2. Set up the project venv and pip-install requirements.
#   3. Install Playwright browsers.
#   4. Restore the most recent pg_dump from S3 into RDS.
#   5. Install cron jobs (run_all, monitor, daily_pipeline, git_autopush).
#   6. Run scripts/validate_production.py as a smoke test.
#
# Required environment / inputs:
#   S3_BUCKET           — e.g. overwatch-aml-backups-<account>-<region>
#   S3_MIGRATION_PREFIX — e.g. migration-20260520T101500Z
#   PG_HOST             — RDS endpoint (.amazonaws.com)
#   PG_USER             — usually "aayush"
#   PG_PASSWORD         — RDS master password (read from secrets manager
#                          or prompt; do NOT bake into this file)
#   PG_DB               — risk_pipeline
#   GITHUB_REPO         — optional, only if running before git clone
#
# Usage (after launching EC2 and SSHing in):
#   sudo -E ./scripts/setup_aws_ec2.sh
# -----------------------------------------------------------------------------

set -euo pipefail

# -----------------------------------------------------------------------------
# 0. Resolve inputs and project root
# -----------------------------------------------------------------------------
if [ "$(id -u)" -ne 0 ]; then
    echo "setup_aws_ec2.sh: re-running with sudo"
    exec sudo -E "$0" "$@"
fi

S3_BUCKET="${S3_BUCKET:-}"
S3_MIGRATION_PREFIX="${S3_MIGRATION_PREFIX:-}"
PG_HOST="${PG_HOST:-}"
PG_USER="${PG_USER:-aayush}"
PG_DB="${PG_DB:-risk_pipeline}"
PG_PASSWORD="${PG_PASSWORD:-}"
PROJECT_USER="${SUDO_USER:-ubuntu}"
PROJECT_HOME="/home/${PROJECT_USER}"
PROJECT_ROOT="${PROJECT_HOME}/risk-pipeline"

for v in S3_BUCKET S3_MIGRATION_PREFIX PG_HOST PG_PASSWORD; do
    if [ -z "${!v:-}" ]; then
        echo "FATAL: required env var $v is empty" >&2
        echo "  example:  S3_BUCKET=overwatch-aml-backups-123-ap-south-1 S3_MIGRATION_PREFIX=migration-... PG_HOST=...rds.amazonaws.com PG_PASSWORD=... ./scripts/setup_aws_ec2.sh" >&2
        exit 2
    fi
done

say()  { printf '\033[1;36m[setup]\033[0m %s\n' "$*"; }
die()  { printf '\033[1;31m[err  ]\033[0m %s\n' "$*" >&2; exit 1; }

# -----------------------------------------------------------------------------
# 1. System dependencies
# -----------------------------------------------------------------------------
say "Step 1/6: installing system dependencies..."
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq \
    python3.12 python3.12-venv python3-pip \
    postgresql-client-16 \
    chromium-browser \
    git curl jq awscli tmux build-essential \
    libpq-dev libxml2-dev libxslt1-dev libnss3 libatk1.0-0 libatk-bridge2.0-0 \
    libcups2 libxcomposite1 libxdamage1 libxrandr2 libgbm1 libxkbcommon0 \
    libasound2t64 fonts-liberation libdrm2 libpango-1.0-0 libcairo2 libnspr4 \
    >/dev/null

# AWS CLI v2 may already be installed; if not, the v1 above is fine for our use.

# -----------------------------------------------------------------------------
# 2. Project repo (clone if missing) + venv + requirements
# -----------------------------------------------------------------------------
say "Step 2/6: project venv + requirements..."
if [ ! -d "$PROJECT_ROOT" ]; then
    if [ -n "${GITHUB_REPO:-}" ]; then
        sudo -u "$PROJECT_USER" git clone "$GITHUB_REPO" "$PROJECT_ROOT"
    else
        die "no project at $PROJECT_ROOT and GITHUB_REPO not set"
    fi
fi
cd "$PROJECT_ROOT"

sudo -u "$PROJECT_USER" python3.12 -m venv venv
sudo -u "$PROJECT_USER" ./venv/bin/pip install --upgrade pip setuptools wheel >/dev/null
if [ -f requirements.txt ]; then
    sudo -u "$PROJECT_USER" ./venv/bin/pip install -r requirements.txt
else
    say "  WARNING: requirements.txt not present; installing common deps"
    sudo -u "$PROJECT_USER" ./venv/bin/pip install \
        psycopg2-binary requests beautifulsoup4 lxml pandas pdfplumber \
        playwright playwright-stealth pgvector
fi

# -----------------------------------------------------------------------------
# 3. Playwright browsers
# -----------------------------------------------------------------------------
say "Step 3/6: installing Playwright browsers..."
sudo -u "$PROJECT_USER" ./venv/bin/playwright install chromium 2>&1 | tail -3 || true

# -----------------------------------------------------------------------------
# 4. Restore DB from S3
# -----------------------------------------------------------------------------
say "Step 4/6: downloading latest pg_dump from S3..."
DUMP_LOCAL="/tmp/risk_pipeline_restore.dump"
DUMP_KEY=$(aws s3 ls "s3://${S3_BUCKET}/${S3_MIGRATION_PREFIX}/db/" \
    | awk '{print $NF}' | grep '\.dump$' | sort | tail -1)
[ -z "$DUMP_KEY" ] && die "no .dump file found under s3://${S3_BUCKET}/${S3_MIGRATION_PREFIX}/db/"
aws s3 cp "s3://${S3_BUCKET}/${S3_MIGRATION_PREFIX}/db/${DUMP_KEY}" "$DUMP_LOCAL"
say "  downloaded: $(du -h "$DUMP_LOCAL" | cut -f1)"

say "Restoring into RDS (postgres://${PG_USER}@${PG_HOST}/${PG_DB})..."
export PGPASSWORD="$PG_PASSWORD"
# Create the database if missing (template database 'postgres' must exist on RDS).
psql -h "$PG_HOST" -U "$PG_USER" -d postgres -tAc \
    "SELECT 1 FROM pg_database WHERE datname='${PG_DB}';" | grep -q 1 \
    || psql -h "$PG_HOST" -U "$PG_USER" -d postgres -c "CREATE DATABASE ${PG_DB};"
# Required extensions before restore.
psql -h "$PG_HOST" -U "$PG_USER" -d "$PG_DB" \
    -c "CREATE EXTENSION IF NOT EXISTS pg_trgm; CREATE EXTENSION IF NOT EXISTS vector; CREATE EXTENSION IF NOT EXISTS pgcrypto;"
# Restore: --no-owner so it doesn't try to assign to the laptop's role.
pg_restore --no-owner --no-privileges --clean --if-exists \
    -h "$PG_HOST" -U "$PG_USER" -d "$PG_DB" --jobs=4 "$DUMP_LOCAL" 2>&1 | tail -10
RESTORED=$(psql -h "$PG_HOST" -U "$PG_USER" -d "$PG_DB" -tAc "SELECT COUNT(*) FROM watchlist_records;")
say "  restored row count: $RESTORED"

# -----------------------------------------------------------------------------
# 5. .env (for run_all.sh) — fill in DB creds + Slack webhook
# -----------------------------------------------------------------------------
say "Step 5/6: writing .env (Slack webhook NOT included; place it manually)"
ENV_FILE="$PROJECT_ROOT/.env"
if [ ! -f "$ENV_FILE" ]; then
    cat > "$ENV_FILE" <<EOF
PG_HOST=${PG_HOST}
PG_USER=${PG_USER}
PG_DB=${PG_DB}
PG_PASSWORD=${PG_PASSWORD}
# SLACK_WEBHOOK_URL=https://hooks.slack.com/services/...  (add manually)
EOF
    chown "$PROJECT_USER:$PROJECT_USER" "$ENV_FILE"
    chmod 600 "$ENV_FILE"
fi

# -----------------------------------------------------------------------------
# 6. Cron jobs (run as PROJECT_USER, not root)
# -----------------------------------------------------------------------------
say "Step 6/6: installing cron jobs as ${PROJECT_USER}..."
CRON_TMP="$(mktemp)"
sudo -u "$PROJECT_USER" crontab -l 2>/dev/null > "$CRON_TMP" || true
grep -v -E 'run_all.sh|monitor_sources.py|daily_pipeline.sh|git_autopush.sh' "$CRON_TMP" > "${CRON_TMP}.clean" || true
cat >> "${CRON_TMP}.clean" <<EOF
0 6 * * * ${PROJECT_ROOT}/run_all.sh >> ${PROJECT_ROOT}/logs/cron.log 2>&1
0 7 * * * ${PROJECT_ROOT}/venv/bin/python ${PROJECT_ROOT}/scripts/monitor_sources.py >> ${PROJECT_ROOT}/logs/monitor_cron.log 2>&1
30 7 * * * ${PROJECT_ROOT}/scripts/daily_pipeline.sh >> ${PROJECT_ROOT}/logs/daily_pipeline_cron.log 2>&1
EOF
sudo -u "$PROJECT_USER" crontab "${CRON_TMP}.clean"
rm -f "$CRON_TMP" "${CRON_TMP}.clean"
sudo -u "$PROJECT_USER" crontab -l

# -----------------------------------------------------------------------------
# Validation
# -----------------------------------------------------------------------------
say "Running validate_production.py as a smoke test..."
mkdir -p "$PROJECT_ROOT/logs" "$PROJECT_ROOT/reports"
chown -R "$PROJECT_USER:$PROJECT_USER" "$PROJECT_ROOT/logs" "$PROJECT_ROOT/reports"
if [ -f "$PROJECT_ROOT/scripts/validate_production.py" ]; then
    sudo -u "$PROJECT_USER" \
        "$PROJECT_ROOT/venv/bin/python" "$PROJECT_ROOT/scripts/validate_production.py" \
        --report "$PROJECT_ROOT/reports/validation_setup.csv" 2>&1 | tail -5
fi

# Final Slack ping if webhook is configured
if [ -f "$PROJECT_ROOT/.claude/settings.local.json" ]; then
    WEBHOOK=$(grep -oE 'https://hooks\.slack\.com/services/[A-Za-z0-9/_-]+' \
                  "$PROJECT_ROOT/.claude/settings.local.json" | head -1 || true)
    if [ -n "${WEBHOOK:-}" ]; then
        curl -s -X POST "$WEBHOOK" -H 'Content-type: application/json' \
            -d "{\"text\":\"🚀 *Overwatch AML* booted on EC2 ($(hostname)). Restored ${RESTORED} records from S3.\"}" >/dev/null
    fi
fi

say "============================================================"
say "EC2 setup complete."
say "  Project dir : $PROJECT_ROOT"
say "  DB rows     : $RESTORED"
say "  Cron jobs   : 3 installed (run_all 06:00, monitor 07:00, daily 07:30)"
say "  Next run    : 06:00 (server local time)"
say "============================================================"
