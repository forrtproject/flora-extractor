#!/usr/bin/env bash
# ----------------------------------------------------------------------------
# discover_example.sh — bash mirror of discover_example.bat.
#
# Same four runs, same env-var knobs, same exit codes. Use whichever one
# matches your shell. The Python entry points are identical so the engine
# behaviour is identical too.
# ----------------------------------------------------------------------------
set -euo pipefail

# User-configurable knobs (override by exporting before running):
MAX_PER_SOURCE="${MAX_PER_SOURCE:-25}"
YEAR_FROM="${YEAR_FROM:-2022}"
YEAR_TO="${YEAR_TO:-2024}"
SOURCES="${SOURCES:-openalex}"
OUT_DIR="${OUT_DIR:-data/examples}"

# Move to repo root no matter where this script was invoked from.
cd "$(dirname "$0")/.."
mkdir -p "$OUT_DIR"

echo
echo "==============================================================================="
echo " FLoRA Extractor — Discover engine walkthrough"
echo " Repo root: $(pwd)"
echo "==============================================================================="
echo

# Step 0a: prerequisites
echo "[Step 0a] Checking Python and required packages..."
python - <<'PY'
import sys, yaml, requests, pandas
print(f"  python {sys.version.split()[0]}")
print(f"  pyyaml {yaml.__version__}")
print(f"  requests {requests.__version__}")
print(f"  pandas {pandas.__version__}")
PY

# Step 0b: API key check
echo
echo "[Step 0b] Checking API keys..."
if [[ -z "${OPENALEX_API_KEY:-}" ]]; then
    echo "  [WARN] OPENALEX_API_KEY not set — OpenAlex will be SKIPPED."
    echo "         Get a free key at https://openalex.org and re-run with:"
    echo "             export OPENALEX_API_KEY=..."
else
    echo "  OK    OPENALEX_API_KEY is set."
fi
[[ -z "${RESEARCHER_EMAIL:-}" ]] && echo "  [WARN] RESEARCHER_EMAIL not set — Crossref polite pool needs it." \
                                  || echo "  OK    RESEARCHER_EMAIL = $RESEARCHER_EMAIL"
[[ -z "${SEMANTIC_SCHOLAR_API_KEY:-}" ]] && echo "  note  SEMANTIC_SCHOLAR_API_KEY not set — S2 falls back to 0.5 req/sec." \
                                          || echo "  OK    SEMANTIC_SCHOLAR_API_KEY is set."

echo
echo "Effective config:"
echo "  MAX_PER_SOURCE = $MAX_PER_SOURCE"
echo "  YEAR_FROM      = $YEAR_FROM"
echo "  YEAR_TO        = $YEAR_TO"
echo "  SOURCES        = $SOURCES"
echo "  OUT_DIR        = $OUT_DIR"
echo

run_engine () {
    local label="$1" file="$2" keywords_arg="$3"
    echo "==============================================================================="
    echo " $label"
    echo "==============================================================================="
    echo
    if [[ -z "$keywords_arg" ]]; then
        python -m search.engine.cli \
            --sources "$SOURCES" \
            --max-per-source "$MAX_PER_SOURCE" \
            --year-from "$YEAR_FROM" --year-to "$YEAR_TO" \
            --out "$OUT_DIR/$file"
    else
        python -m search.engine.cli \
            --keywords "$keywords_arg" \
            --sources "$SOURCES" \
            --max-per-source "$MAX_PER_SOURCE" \
            --year-from "$YEAR_FROM" --year-to "$YEAR_TO" \
            --out "$OUT_DIR/$file"
    fi
    local n
    n=$(($(wc -l < "$OUT_DIR/$file") - 1))
    echo
    echo "Wrote $n rows to $OUT_DIR/$file"
    echo
    eval "$4=$n"
}

# Run 1 — Discover UI "Load example" preset (every wildcard syntax)
run_engine \
    "Run 1  ::  \"Load example\"  (Discover UI's wildcard preset)" \
    "example_load.csv" \
    'replicat*,pre-?registered,(close|high-powered) replication' \
    N1

# Run 2 — Discover UI placeholder text
run_engine \
    "Run 2  ::  \"Placeholder\"  (Discover UI's textarea hint)" \
    "example_placeholder.csv" \
    'replicat*,direct replication,failed to replicate,(close|high-powered|preregistered) replication' \
    N2

# Run 3 — long alternation list, still one OR-bundle per source
run_engine \
    "Run 3  ::  \"Custom\"  (long alternation list, single OR-bundle call)" \
    "example_custom.csv" \
    '(direct|conceptual|preregistered|registered|close|high-powered|systematic|many-labs|exact|near|approximate|external|independent|operational) replication' \
    N3

# Run 4 — spec-only
run_engine \
    "Run 4  ::  \"Spec only\"  (no extra wildcards)" \
    "example_spec.csv" \
    "" \
    N4

# Stage 2 (optional, gracefully skipped on feature/search)
echo "==============================================================================="
echo " Stage 2  ::  filter (rule + LLM)"
echo "==============================================================================="
echo
cp "$OUT_DIR/example_load.csv" "data/candidates.csv"

if python -c "from filter.rule_filter import apply_rule_filter; import inspect; \
import sys; sys.exit(0 if 'NotImplementedError' not in inspect.getsource(apply_rule_filter) else 9)" 2>/dev/null
then
    python -m filter.run_filter || echo "[WARN] filter step failed; continuing."
else
    echo "  note  filter/rule_filter.py is still the stub on this branch."
    echo "        Switch to feature/filter to run Stage 2:"
    echo "            git fetch && git checkout feature/filter"
    echo "            python -m filter.run_filter"
fi

echo
echo "==============================================================================="
echo " Summary"
echo "==============================================================================="
echo "  Run 1 (load example)     $N1 rows  →  $OUT_DIR/example_load.csv"
echo "  Run 2 (placeholder)      $N2 rows  →  $OUT_DIR/example_placeholder.csv"
echo "  Run 3 (custom alt list)  $N3 rows  →  $OUT_DIR/example_custom.csv"
echo "  Run 4 (spec only)        $N4 rows  →  $OUT_DIR/example_spec.csv"
echo
echo " Next steps:"
echo "   -  Open the CSVs in Excel; rows follow CANDIDATES_COLS."
echo "   -  For Stage 2: git checkout feature/filter && python -m filter.run_filter"
echo "   -  Engine internals: docs/scimeto_engine_port.md"
echo "==============================================================================="
