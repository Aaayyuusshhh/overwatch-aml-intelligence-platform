#!/bin/bash
# Auto-commit + push the repo. Includes a size guard: if any
# non-git-ignored file larger than 50MB exists in the working tree,
# the run is aborted (no add/commit/push) and the offending files are
# logged. This prevents GitHub's 100MB hard limit from bricking pushes
# and keeps large DB dumps / data files out of history.
set -u
REPO=/home/aayush/risk-pipeline
cd "$REPO" || exit 1

LOG_DIR="$REPO/logs"
OVERSIZE_LOG="$LOG_DIR/oversized_files.log"
MAX_BYTES=$((50 * 1024 * 1024))   # 50 MB
mkdir -p "$LOG_DIR"

# --- Size guard -----------------------------------------------------
# Find files > 50MB, skipping the .git dir itself. For each, ignore it
# only if git would ignore it (gitignored backups must not block pushes).
oversized_found=0
TS=$(date '+%Y-%m-%d %H:%M:%S')
while IFS= read -r -d '' f; do
    rel="${f#./}"
    # Skip anything git already ignores (e.g. *.dump, data/*.csv).
    if git check-ignore -q -- "$rel"; then
        continue
    fi
    sz=$(stat -c %s "$f" 2>/dev/null || echo 0)
    szmb=$(( sz / 1024 / 1024 ))
    echo "$TS  SKIPPED RUN — oversized (${szmb}MB): $rel" >> "$OVERSIZE_LOG"
    oversized_found=1
done < <(find . -path ./.git -prune -o -type f -size +"${MAX_BYTES}"c -print0)

if [ "$oversized_found" -eq 1 ]; then
    echo "$TS  Auto-push ABORTED: oversized file(s) present (see above)." >> "$OVERSIZE_LOG"
    exit 0
fi
# --------------------------------------------------------------------

git add -A
CHANGES=$(git diff --cached --stat)
if [ -n "$CHANGES" ]; then
    TIMESTAMP=$(date '+%Y-%m-%d %H:%M')
    git commit -m "Auto-update: $TIMESTAMP"
    git push origin main 2>/dev/null || git push origin master 2>/dev/null
fi
