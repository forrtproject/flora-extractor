#!/bin/zsh
# Stage 3 campaign: 1,325 works from release bc38ddd787e0.
# Waits until just past midnight UTC (fresh OpenAlex quota), then runs the
# extract tier and renders data/extracted.csv. Scheduled 2026-08-07.
set -u

cd /Users/lukaswallrich/Documents/Coding/flora-extractor || exit 1

TARGET_EPOCH=$1        # unix timestamp to start at
LOG=logs/stage3-campaign-2026-08-07.log

{
  echo "=== scheduled $(date -u '+%F %T UTC') — waiting until $(date -u -r "$TARGET_EPOCH" '+%F %T UTC') ==="
} >> "$LOG" 2>&1

while [ "$(date +%s)" -lt "$TARGET_EPOCH" ]; do
  sleep 30
done

{
  echo "=== tier start $(date -u '+%F %T UTC') ==="
  if .venv/bin/python -m extract.tier --run \
        --release bc38ddd787e0 \
        --batch-label stage3-2026-08-07; then
    echo "=== tier done $(date -u '+%F %T UTC') ==="
    # Only a COMPLETE tier run may re-render the CSV. The export writes whole,
    # from the current generation's verdicts, so exporting after a crash replaces
    # every row with the handful the crashed run had settled — which is how
    # extracted.csv went from 285 rows to 6 on 2026-08-07.
    echo "=== export start $(date -u '+%F %T UTC') ==="
    .venv/bin/python -m extract.export
    echo "=== export exit $? at $(date -u '+%F %T UTC') ==="
  else
    echo "=== tier FAILED ($?) at $(date -u '+%F %T UTC') — export skipped ==="
  fi
} >> "$LOG" 2>&1
