@echo off
REM ============================================================================
REM  pipeline_example.bat  --  walk through the full FLoRA Extractor pipeline,
REM  with a particular focus on Stage 2, the declarative filter engine.
REM
REM  What this script does, in plain English:
REM
REM    1. Verifies your Python + dependency environment.
REM    2. Stage 1: builds a candidates.csv. By default uses a synthetic
REM       fixture so the demo is reproducible and free.  Set LIVE_SEARCH=1 to
REM       fetch a small live window from OpenAlex / S2 / I4R instead.
REM    3. Stage 2: `python -m filter.engine`.  The engine routes the SURVIVOR
REM       POOL (parquet under cache\snapshot_pool) through the spec bundle in
REM       filter\spec\ -- it does NOT read candidates.csv, so Stage 1's output
REM       above cannot be chained into it.  The script always prints the
REM       bundle (`specs`, offline and free); if a pool is present it also
REM       routes it and dry-runs the expensive screen tier, which claims,
REM       fetches and spends nothing.
REM    4. Tells you the exact commands for Stage 3 (extract) and Stage 4
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
REM  data\examples, not data\: the demo must never overwrite a real
REM  data\candidates.csv, which can be a million rows someone spent hours on.
if "%OUT_DIR%"==""       set OUT_DIR=data\examples
if "%POOL_DIR%"==""      set POOL_DIR=cache\snapshot_pool

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
REM  Step 0a: prerequisites.  Stage 2 needs pyarrow + duckdb (the engine reads
REM  parquet and caches routing in DuckDB).  Pandas is needed throughout.
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
    echo         Stage 2's LLM tiers cannot run; routing is fully offline.
) else (
    echo   OK    LLM key configured -- the screen tiers can be run with --run.
)

echo.

REM Make sure the data directory exists; it's gitignored so demo outputs don't
REM accidentally get committed.
if not exist "%OUT_DIR%" mkdir "%OUT_DIR%"

echo Effective config:
echo   LIVE_SEARCH = %LIVE_SEARCH%   (1 = call APIs; 0 = synthetic fixture)
echo   YEAR_FROM   = %YEAR_FROM%
echo   YEAR_TO     = %YEAR_TO%
echo   OUT_DIR     = %OUT_DIR%
echo   POOL_DIR    = %POOL_DIR%
echo.

REM ===========================================================================
REM  Stage 1  --  build data\candidates.csv
REM ===========================================================================
echo ===============================================================================
echo  Stage 1  ::  search  (-^> %OUT_DIR%\candidates.csv)
echo ===============================================================================
echo.
if "%LIVE_SEARCH%"=="0" (
    echo Using a synthetic 5-row fixture ^(examples\_make_fixture.py^).
    echo The rows cover the range Stage 2 has to tell apart: clear replication,
    echo reproduction, no-cite, DNA exclusion, and no-phrase.
    set CANDIDATES=%OUT_DIR%\candidates.csv
    python examples\_make_fixture.py "%OUT_DIR%\candidates.csv"
    if errorlevel 1 (
        echo [ERROR] could not generate fixture.
        popd
        exit /b 3
    )
) else (
    echo Calling OpenAlex / Semantic Scholar / I4R via the per-source scripts
    echo with --from-year %YEAR_FROM% --to-year %YEAR_TO%.  Caching kicks in
    echo automatically; reruns within the cache TTL are free.
    echo NOTE: a live run merges into the real data\candidates.csv.
    set CANDIDATES=data\candidates.csv
    python -m search.run_search --from-year %YEAR_FROM% --to-year %YEAR_TO%
    if errorlevel 1 (
        echo [ERROR] Stage 1 live search failed.
        popd
        exit /b 3
    )
)
for /f %%a in ('type "!CANDIDATES!" ^| find /v /c ""') do set N1=%%a
set /a N1-=1
echo.
echo Stage 1 produced %N1% candidate rows.
echo.

REM ===========================================================================
REM  Stage 2  --  the filter engine (`python -m filter.engine`)
REM
REM    -  `specs` lists the declarative bundle in filter\spec\ with its hash.
REM       Offline: no pool, no keys, no spend.
REM    -  `route` streams the survivor pool through the bundle and routes
REM       every row into a pile (discard / screen_expensive / screen_cheap /
REM       needs_human / pending).  Rules route and discard; only LLMs admit.
REM    -  `screen --tier screen_expensive` is a DRY RUN unless you pass
REM       --run: it prints the pile size and an estimate and spends nothing.
REM    -  `handoff` writes the screen piles as Stage 3's data\filtered.csv.
REM
REM  Design: docs\filter-engine.md.  Every flag: docs\cli-reference.md.
REM ===========================================================================
echo ===============================================================================
echo  Stage 2  ::  filter engine  (route -^> screen -^> handoff -^> data\filtered.csv)
echo ===============================================================================
echo.
echo The bundle Stage 2 routes with (offline; no pool, no keys, no spend):
echo.

python -m filter.engine specs
if errorlevel 1 (
    echo [ERROR] could not load the spec bundle.
    popd
    exit /b 4
)
echo.

if exist "%POOL_DIR%" (
    echo Pool found at %POOL_DIR% -- routing it into a release in the local store.
    echo.
    python -m filter.engine route --pool "%POOL_DIR%"
    echo.
    echo What the expensive ^(two-voter classify^) tier would cost over its pile.
    echo No --run: nothing is claimed, fetched or spent.
    echo.
    python -m filter.engine screen --tier screen_expensive --pool "%POOL_DIR%"
    echo.
    echo To actually spend, and then to write Stage 3's input:
    echo.
    echo     python -m filter.engine screen --tier screen_expensive --run
    echo     python -m filter.engine handoff --out %OUT_DIR%\filtered.csv
) else (
    echo No survivor pool at %POOL_DIR% -- Stage 2 has nothing to route.
    echo Fetch one, then run the three commands:
    echo.
    echo     python -m search.pool_sync --pull
    echo     python -m filter.engine route
    echo     python -m filter.engine screen --tier screen_expensive --run
    echo     python -m filter.engine handoff --out %OUT_DIR%\filtered.csv
    echo.
    echo Every flag: docs\cli-reference.md Stage 2.  Design: docs\filter-engine.md.
)

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
echo   Stage 1 candidates  ::  %N1% rows  --  !CANDIDATES!
echo   Stage 2 filtered    ::               --  %OUT_DIR%\filtered.csv (via handoff)
echo.
echo  For the OR-bundled Stage 1 engine demo, run:
echo      examples\discover_example.bat
echo.
echo  Stage 2 internals: docs\filter-engine.md  --  all flags: docs\cli-reference.md
echo ===============================================================================

popd
endlocal
exit /b 0
