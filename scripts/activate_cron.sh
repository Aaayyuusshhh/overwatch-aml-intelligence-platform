#!/usr/bin/env bash
# Idempotent installer that adds the risk-pipeline daily run to the
# current user's crontab. The job runs at 06:00 IST every day; output
# is appended to logs/cron.log. Re-running this script does not create
# duplicates — it filters any existing line matching run_all.sh first.
#
# Usage:
#   ./scripts/activate_cron.sh
# Verify with:
#   crontab -l
# Remove later with:
#   crontab -l | grep -v 'risk-pipeline/run_all.sh' | crontab -

set -euo pipefail

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
RUNNER="$PROJECT_ROOT/run_all.sh"
CRON_LOG="$PROJECT_ROOT/logs/cron.log"
CRON_LINE="0 6 * * * $RUNNER >> $CRON_LOG 2>&1"

if [ ! -x "$RUNNER" ]; then
    echo "activate_cron.sh: $RUNNER is not executable" >&2
    exit 1
fi

EXISTING=$(crontab -l 2>/dev/null || true)
FILTERED=$(printf '%s\n' "$EXISTING" | grep -v 'risk-pipeline/run_all.sh' || true)
{ printf '%s\n' "$FILTERED"; printf '%s\n' "$CRON_LINE"; } \
    | sed '/^$/d' | crontab -

echo "Installed cron entry:"
echo "  $CRON_LINE"
echo
echo "Current crontab:"
crontab -l
