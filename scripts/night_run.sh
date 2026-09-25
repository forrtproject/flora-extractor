#!/usr/bin/env bash
# Night run: screen the admitted pile, then extract the new proceeds — both at OpenAI
# flex tier, which is refused in waves during the (European) day and mostly served
# at night. Scheduled by systemd-run (see PENDING_RUNS.md); safe to run by hand.
#
#   scripts/night_run.sh <release-id> [extract-limit]
#
# Refuses to start on a dirty code tree: a scheduled run must run committed code,
# never an agent's half-finished edit.
set -uo pipefail
cd "$(dirname "$0")/.."

RELEASE="${1:?release id}"
LIMIT="${2:-}"
STAMP="$(date -u +%Y%m%dT%H%MZ)"
LOG="logs/night_run_${STAMP}.log"
mkdir -p logs

export OPENAI_DAILY_TOKEN_BUDGET=0
export OPENAI_FLEX_PATIENCE=600

{
  echo "== night run ${STAMP} release ${RELEASE} at $(git rev-parse --short HEAD)"
  if [ -n "$(git status --porcelain --untracked-files=no -- shared extract filter search)" ]; then
    echo "REFUSED: uncommitted changes under shared/ extract/ filter/ search/:"
    git status --porcelain --untracked-files=no -- shared extract filter search
    exit 2
  fi

  echo "== screen dry run"
  .venv/bin/python -m filter.engine screen --tier screen_expensive --release "$RELEASE" 2>&1 \
    | grep -v "INFO\|WARNING"
  echo "== screen"
  .venv/bin/python -m filter.engine screen --tier screen_expensive --release "$RELEASE" --run
  echo "screen exit $?"

  echo "== extract dry run"
  .venv/bin/python -m extract.tier --release "$RELEASE" ${LIMIT:+--limit "$LIMIT"} 2>&1 \
    | grep -v "INFO\|WARNING"
  echo "== extract"
  .venv/bin/python -m extract.tier --run --release "$RELEASE" \
    --batch-label "night-${STAMP}" ${LIMIT:+--limit "$LIMIT"}
  echo "extract exit $?"
  echo "== done $(date -u +%Y%m%dT%H%MZ)"
} >> "$LOG" 2>&1
