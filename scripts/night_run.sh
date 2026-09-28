#!/usr/bin/env bash
# Night run: screen the admitted pile, then extract the new proceeds — both at OpenAI
# flex tier, which is refused in waves during the (European) day and mostly served
# at night. Scheduled by systemd-run (see PENDING_RUNS.md); safe to run by hand.
#
#   scripts/night_run.sh <release-id> [extract-limit]
#
# Refuses to start on a dirty code tree: a scheduled run must run committed code,
# never an agent's half-finished edit.
#
# Start it under a memory cap, always:
#
#   systemd-run --user -p MemoryMax=8G -p MemorySwapMax=0 [--on-calendar=…] \
#     scripts/night_run.sh <release-id>
#
# The box has 15 GB. On 2026-09-27 the extract grew past 14 GB and thrashed the whole
# machine until a hard reboot; capped, a runaway run is OOM-killed inside its own
# unit, its claims lapse after the lease, and the next run resumes from the verdicts.
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

  # A sandbox batch gates the live run whenever Stage 3 code changed since the last
  # night run (CLAUDE.md "Before a Run That Spends", rule 1). A crash stops the night;
  # running out of OpenAlex budget is not a crash.
  STAGE3_REV="$(git log -1 --format=%h -- extract shared)"
  if [ "$(cat logs/.night_run_stage3_rev 2>/dev/null)" != "$STAGE3_REV" ]; then
    echo "== sandbox gate: Stage 3 code changed (${STAGE3_REV})"
    SB="logs/night_run_${STAMP}_sandbox.log"
    .venv/bin/python -m extract.tier --run --release "$RELEASE" --mode validation \
      --limit 30 --batch-label "night-gate-${STAMP}" > "$SB" 2>&1
    rc=$?
    grep -v "INFO\|WARNING" "$SB" | tail -12
    if [ $rc -ne 0 ] && ! grep -q "OpenAlexQuotaExhausted" "$SB"; then
      echo "REFUSED: the sandbox batch failed (exit $rc); see $SB"
      exit 3
    fi
    echo "$STAGE3_REV" > logs/.night_run_stage3_rev
  fi

  echo "== extract dry run"
  .venv/bin/python -m extract.tier --release "$RELEASE" ${LIMIT:+--limit "$LIMIT"} 2>&1 \
    | grep -v "INFO\|WARNING"
  echo "== extract"
  .venv/bin/python -m extract.tier --run --release "$RELEASE" \
    --batch-label "night-${STAMP}" ${LIMIT:+--limit "$LIMIT"}
  echo "extract exit $?"
  echo "== done $(date -u +%Y%m%dT%H%MZ)"
} >> "$LOG" 2>&1
