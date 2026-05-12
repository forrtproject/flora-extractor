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
OUT_DIR="${OUT_DIR:-data}"

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
    echo "        Stage 2 LLM uplift will be a no-op; rule filter still runs."
else
    echo "  OK    LLM key configured — needs_review rows will be uplifted."
fi

echo
echo "Effective config:"
echo "  LIVE_SEARCH = $LIVE_SEARCH"
echo "  YEAR_FROM   = $YEAR_FROM"
echo "  YEAR_TO     = $YEAR_TO"
echo "  OUT_DIR     = $OUT_DIR"
echo

echo "==============================================================================="
echo " Stage 1  ::  search  (→ data/candidates.csv)"
echo "==============================================================================="
echo
if [[ "$LIVE_SEARCH" == "0" ]]; then
    echo "Generating a 5-row synthetic fixture (examples/_make_fixture.py)."
    python examples/_make_fixture.py "$OUT_DIR/candidates.csv"
else
    echo "Calling Amy's per-source scripts with --from-year $YEAR_FROM --to-year $YEAR_TO."
    python -m search.run_search --from-year "$YEAR_FROM" --to-year "$YEAR_TO"
fi
N1=$(($(wc -l < "$OUT_DIR/candidates.csv") - 1))
echo
echo "Stage 1 produced $N1 candidate rows."
echo

echo "==============================================================================="
echo " Stage 2  ::  filter  (rule_filter.py → llm_filter.py → data/filtered.csv)"
echo "==============================================================================="
echo
python -m filter.run_filter
echo
python - <<PY
import pandas as pd
df = pd.read_csv(r"$OUT_DIR/filtered.csv", encoding="utf-8-sig")
print()
print("filter_status breakdown:")
print(df["filter_status"].value_counts().to_string())
print()
print("first 5 rows:")
print(df[["doi_r","filter_status","filter_method","filter_evidence"]].head().to_string(index=False))
PY
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
echo "    python -m validate.import_csv"
echo "    python -m validate.app"
echo
echo "==============================================================================="
echo " Summary"
echo "==============================================================================="
echo "  Stage 1 candidates :: $N1 rows  →  $OUT_DIR/candidates.csv"
echo "  Stage 2 filtered   ::              →  $OUT_DIR/filtered.csv"
echo
echo " Engine-based Stage 1 (the SciMeto Discover-UI port) lives on feature/search."
echo " Run examples/discover_example.sh there for the OR-bundled engine demo."
echo
echo " Stage 2 internals: docs/scimeto_filter_port.md"
