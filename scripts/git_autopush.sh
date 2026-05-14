#!/bin/bash
cd /home/aayush/risk-pipeline
git add -A
CHANGES=$(git diff --cached --stat)
if [ -n "$CHANGES" ]; then
    TIMESTAMP=$(date '+%Y-%m-%d %H:%M')
    git commit -m "Auto-update: $TIMESTAMP"
    git push origin main 2>/dev/null || git push origin master 2>/dev/null
fi
