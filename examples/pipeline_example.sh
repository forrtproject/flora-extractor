#!/usr/bin/env bash
# ----------------------------------------------------------------------------
# pipeline_example.sh — bash mirror of pipeline_example.bat.
#
# Same env-var knobs, same Python entry points. Use whichever one your shell
# prefers. Defaults to the bundled sample so the demo is offline-safe.
# ----------------------------------------------------------------------------
set -euo pipefail

LIVE_SEARCH="${LIVE_SEARCH:-0}"
YEAR_FROM="${YEAR_FROM:-2023}"
YEAR_TO="${YEAR_TO:-2024}"
# data/examples, not data/: the demo must never overwrite a real
# data/candidates.csv, which can be a million rows someone spent hours building.
OUT_DIR="${OUT_DIR:-data/examples}"
POOL_DIR="${FLORA_POOL_DIR:-cache/snapshot_pool}"

cd "$(dirname "$0")/.."
mkdir -p "$OUT_DIR"

echo
echo "==============================================================================="
echo " FLoRA Extractor — Pipeline walkthrough  (Stage 1 → 2; pointers for 3 + 4)"
echo " Repo root: $(pwd)"
echo "==============================================================================="
echo

echo "[Step 0a] Checking Python and required packages..."
python - <<'PY'
import sys, yaml, requests, pandas
print(f"  python {sys.version.split()[0]}")
print(f"  pyyaml {yaml.__version__}")
print(f"  requests {requests.__version__}")
print(f"  pandas {pandas.__version__}")
PY

echo
echo "[Step 0b] Checking API keys..."
if [[ "$LIVE_SEARCH" != "0" ]]; then
    [[ -z "${OPENALEX_API_KEY:-}" ]] \
        && echo "  [WARN] OPENALEX_API_KEY not set — OpenAlex calls will fail." \
        || echo "  OK    OPENALEX_API_KEY is set."
fi
if [[ -z "${GEMINI_API_KEY:-}" && -z "${OPENAI_API_KEY:-}" ]]; then
    echo "  note  No GEMINI_API_KEY / OPENAI_API_KEY set."
    echo "        Stage 2's LLM tiers cannot run; routing is fully offline."
else
    echo "  OK    LLM key configured — the screen tiers can be run with --run."
fi

echo
echo "Effective config:"
echo "  LIVE_SEARCH = $LIVE_SEARCH"
echo "  YEAR_FROM   = $YEAR_FROM"
echo "  YEAR_TO     = $YEAR_TO"
echo "  OUT_DIR     = $OUT_DIR"
echo "  POOL_DIR    = $POOL_DIR"
echo

echo "==============================================================================="
echo " Stage 1  ::  search  (→ $OUT_DIR/candidates.csv)"
echo "==============================================================================="
echo
if [[ "$LIVE_SEARCH" == "0" ]]; then
    echo "Generating a 5-row synthetic fixture (examples/_make_fixture.py)."
    CANDIDATES="$OUT_DIR/candidates.csv"
    python examples/_make_fixture.py "$CANDIDATES"
else
    # run_search always writes the real data/candidates.csv, merging into it.
    echo "Calling the per-source scripts with --from-year $YEAR_FROM --to-year $YEAR_TO."
    echo "NOTE: a live run merges into the real data/candidates.csv."
    CANDIDATES="data/candidates.csv"
    python -m search.run_search --from-year "$YEAR_FROM" --to-year "$YEAR_TO"
fi
N1=$(($(wc -l < "$CANDIDATES") - 1))
echo
echo "Stage 1 produced $N1 candidate rows."
echo

echo "==============================================================================="
echo " Stage 2  ::  filter engine  (route → screen → handoff → data/filtered.csv)"
echo "==============================================================================="
echo
# Stage 2 is `python -m filter.engine`. It routes the SURVIVOR POOL (parquet)
# through the declarative spec bundle in filter/spec/ — it does not read
# candidates.csv, so the Stage 1 fixture above cannot be chained into it.
echo "The bundle Stage 2 routes with (offline; no pool, no keys, no spend):"
echo
python -m filter.engine specs
echo

if [[ -d "$POOL_DIR" ]]; then
    echo "Pool found at $POOL_DIR — routing it into a release in the local store."
    echo
    python -m filter.engine route --pool "$POOL_DIR"
    echo
    echo "What the expensive (two-voter classify) tier would cost over its pile."
    echo "No --run: nothing is claimed, fetched or spent."
    echo
    python -m filter.engine screen --tier screen_expensive --pool "$POOL_DIR"
    echo
    echo "To actually spend, and then to write Stage 3's input:"
    echo
    echo "    python -m filter.engine screen --tier screen_expensive --run"
    echo "    python -m filter.engine handoff --out $OUT_DIR/filtered.csv"
else
    echo "No survivor pool at $POOL_DIR — Stage 2 has nothing to route."
    echo "Fetch one, then run the three commands:"
    echo
    echo "    python -m search.pool_sync --pull"
    echo "    python -m filter.engine route"
    echo "    python -m filter.engine screen --tier screen_expensive --run"
    echo "    python -m filter.engine handoff --out $OUT_DIR/filtered.csv"
    echo
    echo "Every flag: docs/cli-reference.md §Stage 2. Design: docs/filter-engine.md."
fi
echo

echo "==============================================================================="
echo " Stage 3  ::  extract  (LLM-heavy; not auto-run)"
echo "==============================================================================="
echo
echo "    python -m extract.run_extract"
echo
echo "==============================================================================="
echo " Stage 4  ::  validate  (Flask web app on http://localhost:5001)"
echo "==============================================================================="
echo
echo "    python -m extract.csv_to_db"
echo "    python -m validate.app"
echo
echo "==============================================================================="
echo " Summary"
echo "==============================================================================="
echo "  Stage 1 candidates :: $N1 rows  →  $CANDIDATES"
echo "  Stage 2 filtered   ::              →  $OUT_DIR/filtered.csv (via handoff)"
echo
echo " For the OR-bundled Stage 1 engine demo: examples/discover_example.sh"
echo
echo " Stage 2 internals: docs/filter-engine.md · all flags: docs/cli-reference.md"
