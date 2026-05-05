@echo off
REM ============================================================================
REM  pipeline_example.bat  --  walk through the full FLoRA Extractor pipeline,
REM  with a particular focus on Stage 2 (filter), the SciMeto-classifier port
REM  that landed on this branch.
REM
REM  What this script does, in plain English:
REM
REM    1. Verifies your Python + dependency environment.
REM    2. Stage 1: builds a candidates.csv. By default uses the bundled sample
REM       at misc/sample_candidates.csv so the demo is reproducible and free.
REM       Set LIVE_SEARCH=1 to fetch a small live window from OpenAlex / S2 /
REM       I4R via Amy's per-source scripts instead.
REM    3. Stage 2: runs `python -m filter.run_filter`, which applies the
REM       rule-based classifier (phrase detection + author-year cite gate +
REM       non-scholarly exclusion) and then the LLM uplift on rows the rules
REM       left as needs_review.  If no LLM key is configured the LLM step is
REM       a no-op (rows stay needs_review rather than getting fake verdicts).
REM    4. Reports the breakdown of filter_status values so you can see how
REM       the rule / LLM steps actually classified the sample.
REM    5. Tells you the exact commands for Stage 3 (extract) and Stage 4
REM       (validate web app), but does NOT run them automatically -- both are
REM       LLM-heavy and live, so we don't want to surprise-bill anyone.
REM
REM  Conservative defaults:
REM
REM    LIVE_SEARCH=0       use bundled sample (4 rows; instant, free)
REM    OUT_DIR=data        Stage 1/2 outputs (gitignored)
REM
REM  When LIVE_SEARCH=1, also set:
REM
REM    YEAR_FROM (default 2023) and YEAR_TO (default 2024) so the live search
REM    pulls a tight window. Amy's scripts forward both flags to OpenAlex,
REM    Semantic Scholar, and the I4R adapter as of cde352c.
REM
REM  Cross-platform: pipeline_example.sh mirrors this script for bash users.
REM ============================================================================

setlocal EnableDelayedExpansion

REM ---------------------------------------------------------------------------
REM  User-configurable knobs.
REM ---------------------------------------------------------------------------
if "%LIVE_SEARCH%"==""   set LIVE_SEARCH=0
if "%YEAR_FROM%"==""     set YEAR_FROM=2023
if "%YEAR_TO%"==""       set YEAR_TO=2024
if "%OUT_DIR%"==""       set OUT_DIR=data

REM Move into the repo root regardless of where the user invoked this from.
pushd "%~dp0\.."
if errorlevel 1 (
    echo [ERROR] could not change to repo root.
    exit /b 1
)

echo.
echo ===============================================================================
echo  FLoRA Extractor  --  Pipeline walkthrough  (Stage 1 -^> 2; pointers for 3 + 4)
echo  Repo root:  %CD%
echo ===============================================================================
echo.

REM ---------------------------------------------------------------------------
REM  Step 0a: prerequisites.  Stage 2 needs pyyaml (loads filter\spec\
REM  exclusion-patterns.yaml at import time).  Pandas is needed throughout.
REM ---------------------------------------------------------------------------
echo [Step 0a]  Checking Python and required packages...
python -c "import sys, yaml, requests, pandas; print(f'  python {sys.version.split()[0]}'); print(f'  pyyaml {yaml.__version__}'); print(f'  requests {requests.__version__}'); print(f'  pandas {pandas.__version__}')" 2>&1
if errorlevel 1 (
    echo.
    echo [ERROR] missing packages.  Run:    pip install -r requirements.txt
    popd
    exit /b 2
)

echo.

REM ---------------------------------------------------------------------------
REM  Step 0b: API key check.  Only relevant for live search and the LLM step.
REM  Filter rules + exclusion regex run fully offline.
REM ---------------------------------------------------------------------------
echo [Step 0b]  Checking API keys...
if not "%LIVE_SEARCH%"=="0" (
    if "%OPENALEX_API_KEY%"=="" (
        echo   [WARN] OPENALEX_API_KEY is NOT set -- OpenAlex calls will fail.
        echo          Either set the key or unset LIVE_SEARCH for the offline demo.
    ) else (
        echo   OK    OPENALEX_API_KEY is set ^(live search enabled^).
    )
)
if "%GEMINI_API_KEY%"=="" if "%OPENAI_API_KEY%"=="" (
    echo   note  No GEMINI_API_KEY or OPENAI_API_KEY set.
    echo         Stage 2 LLM uplift will be a no-op; rule filter still runs.
) else (
    echo   OK    LLM key configured -- needs_review rows will be uplifted.
)

echo.

REM Make sure the data directory exists; it's gitignored so demo outputs don't
REM accidentally get committed.
if not exist "%OUT_DIR%" mkdir "%OUT_DIR%"

echo Effective config:
echo   LIVE_SEARCH = %LIVE_SEARCH%   (1 = call APIs; 0 = use misc/sample_candidates.csv)
echo   YEAR_FROM   = %YEAR_FROM%
echo   YEAR_TO     = %YEAR_TO%
echo   OUT_DIR     = %OUT_DIR%
echo.

REM ===========================================================================
REM  Stage 1  --  build data\candidates.csv
REM ===========================================================================
echo ===============================================================================
echo  Stage 1  ::  search  (-^> data\candidates.csv)
echo ===============================================================================
echo.
if "%LIVE_SEARCH%"=="0" (
    echo Using a synthetic 5-row fixture ^(examples\_make_fixture.py^).
    echo The rows cover every Stage-2 path: clear replication, reproduction,
    echo no-cite needs_review, DNA exclusion, and no-phrase false_positive.
    python examples\_make_fixture.py "%OUT_DIR%\candidates.csv"
    if errorlevel 1 (
        echo [ERROR] could not generate fixture.
        popd
        exit /b 3
    )
) else (
    echo Calling OpenAlex / Semantic Scholar / I4R via Amy's per-source scripts
    echo with --from-year %YEAR_FROM% --to-year %YEAR_TO%.  Caching kicks in
    echo automatically; reruns within the cache TTL are free.
    python -m search.run_search --from-year %YEAR_FROM% --to-year %YEAR_TO%
    if errorlevel 1 (
        echo [ERROR] Stage 1 live search failed.
        popd
        exit /b 3
    )
)
for /f %%a in ('type "%OUT_DIR%\candidates.csv" ^| find /v /c ""') do set N1=%%a
set /a N1-=1
echo.
echo Stage 1 produced %N1% candidate rows.
echo.

REM ===========================================================================
REM  Stage 2  --  filter (rule + LLM)
REM
REM  filter\rule_filter.py is the SciMeto phrase-detection port:
REM
REM    -  Reads filter\spec\exclusion-patterns.yaml and drops rows whose
REM       title+abstract matches a non-scholarly context (DNA replication,
REM       code/data replication, replication fork/origin/stress/timing).
REM    -  Detects 19 replication phrases (REPLICATION_PHRASES in
REM       filter\phrase_detection.py).
REM    -  Requires an explicit author-year citation in the same text for a
REM       high-confidence "replication" or "reproduction" verdict.  Without
REM       a cite, rows go to "needs_review".
REM
REM  filter\llm_filter.py then asks Gemini (then OpenAI) to classify the
REM  remaining needs_review rows in JSON mode, with caching by hash of
REM  title+abstract.  No keys = no LLM step = needs_review stays.
REM ===========================================================================
echo ===============================================================================
echo  Stage 2  ::  filter  (rule_filter.py -^> llm_filter.py -^> data\filtered.csv)
echo ===============================================================================
echo.

python -m filter.run_filter
if errorlevel 1 (
    echo [ERROR] Stage 2 failed.
    popd
    exit /b 4
)
echo.

REM Pull the filter_status counts directly from the CSV so the user can see
REM the breakdown without opening the file.
python -c "import pandas as pd; df = pd.read_csv(r'%OUT_DIR%\filtered.csv', encoding='utf-8-sig'); print(); print('filter_status breakdown:'); print(df['filter_status'].value_counts().to_string()); print(); print('first 5 rows (status, evidence):'); print(df[['doi_r','filter_status','filter_method','filter_evidence']].head().to_string(index=False))" 2>&1

echo.

REM ===========================================================================
REM  Stage 3  --  extract  (NOT auto-run; LLM-heavy)
REM ===========================================================================
echo ===============================================================================
echo  Stage 3  ::  extract  (LLM-heavy; not auto-run)
echo ===============================================================================
echo.
echo To run Stage 3 against the filtered.csv from Stage 2:
echo.
echo     python -m extract.run_extract
echo.
echo Stage 3 calls Gemini / OpenRouter for each "replication" or "reproduction"
echo row.  Cache is at cache\llm\.  See extract\run_extract.py for routing
echo (single_original vs multi_original) and the streamed CSV writer.
echo.

REM ===========================================================================
REM  Stage 4  --  validate web app
REM ===========================================================================
echo ===============================================================================
echo  Stage 4  ::  validate  (Flask web app on http://localhost:5001)
echo ===============================================================================
echo.
echo To launch the web app:
echo.
echo     python -m validate.import_csv
echo     python -m validate.app
echo.
echo Then open http://localhost:5001/.  The new tabs (since merge of #21):
echo     /search    Stage 1 candidates
echo     /filter    Stage 2 filtered list
echo     /extract   Stage 3 extraction with model-comparison tool
echo     /validate  Stage 4 voting queue
echo.

REM ===========================================================================
REM  Summary
REM ===========================================================================
echo ===============================================================================
echo  Summary
echo ===============================================================================
echo   Stage 1 candidates  ::  %N1% rows  --  %OUT_DIR%\candidates.csv
echo   Stage 2 filtered    ::               --  %OUT_DIR%\filtered.csv
echo.
echo  Engine-based Stage 1 (the SciMeto Discover-UI port) lives on
echo  feature/search.  Switch branches and run:
echo      examples\discover_example.bat
echo  to see the OR-bundled engine version that uses search\spec\*.yaml.
echo.
echo  Stage 2 internals: docs\scimeto_filter_port.md
echo ===============================================================================

popd
endlocal
exit /b 0
