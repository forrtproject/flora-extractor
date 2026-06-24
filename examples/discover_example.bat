@echo off
REM ============================================================================
REM  discover_example.bat  --  walk through Stage 1 (search) of the FLoRA
REM  Extractor pipeline using the YAML-spec Discover engine ported from
REM  SciMeto.
REM
REM  What this script does, in plain English:
REM
REM    1. Verifies your Python + dependency environment.
REM    2. Confirms the OpenAlex API key is set (required since Feb 13, 2026).
REM    3. Runs three demonstration searches against OpenAlex / Crossref /
REM       Semantic Scholar with progressively larger keyword sets:
REM
REM         a) "Load example"  -- the same three keywords the SciMeto Discover
REM            UI ships behind its "Load example" button. Exercises every
REM            wildcard syntax (trailing *, optional ?, alternation).
REM
REM         b) "Placeholder"   -- the four keywords shown as the placeholder in
REM            the New Run modal (slightly broader; uses two phrase literals).
REM
REM         c) "Custom"        -- a longer alternation list to demonstrate how
REM            the engine bundles many phrase variants into ONE OpenAlex
REM            search call (the cost-saving point of the port).
REM
REM    4. After each run, shows where the output CSV landed and a quick row
REM       count so you can see how recall changes with the keyword set.
REM
REM    5. Optionally runs Stage 2 (filter) on the engine output.  Stage 2
REM       lives on feature/filter, so this step gracefully skips with a
REM       message when run from feature/search.
REM
REM  Conservative defaults are used everywhere:
REM
REM    --max-per-source 25        keep the demo runs cheap
REM    --year-from 2022 --year-to 2024  three-year window, fast pagination
REM    --sources openalex         only one source by default (extend with
REM                               --sources openalex,crossref,semantic_scholar)
REM
REM  Override any of these by setting the matching env var BEFORE invoking
REM  this script. See the "User-configurable knobs" block below.
REM
REM  Cross-platform: discover_example.sh mirrors this script for bash users.
REM  The Python entry points and arguments are identical across both files.
REM ============================================================================

setlocal EnableDelayedExpansion

REM ---------------------------------------------------------------------------
REM  User-configurable knobs.  Override by setting the env var beforehand,
REM  e.g.:    set MAX_PER_SOURCE=10 && examples\discover_example.bat
REM ---------------------------------------------------------------------------
if "%MAX_PER_SOURCE%"=="" set MAX_PER_SOURCE=25
if "%YEAR_FROM%"==""      set YEAR_FROM=2022
if "%YEAR_TO%"==""        set YEAR_TO=2024
if "%SOURCES%"==""        set SOURCES=openalex
if "%OUT_DIR%"==""        set OUT_DIR=data\examples

REM ---------------------------------------------------------------------------
REM  Move into the repo root regardless of where the user invoked this from.
REM  %~dp0 is the directory containing this .bat (examples\), so .. is repo.
REM ---------------------------------------------------------------------------
pushd "%~dp0\.."
if errorlevel 1 (
    echo [ERROR] could not change to repo root.
    exit /b 1
)

echo.
echo ===============================================================================
echo  FLoRA Extractor  --  Discover engine walkthrough
echo  Repo root:  %CD%
echo ===============================================================================
echo.

REM ---------------------------------------------------------------------------
REM  Step 0a: prerequisites  --  Python + pyyaml + requests + pandas.
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
REM  Step 0b: API key check.  Since 2026-02-13 OpenAlex requires a key for
REM  any non-trivial search use; mailto-only no longer authenticates the
REM  polite pool.  See search\RATE_LIMITS_VERIFIED.md.
REM ---------------------------------------------------------------------------
echo [Step 0b]  Checking API keys...
if "%OPENALEX_API_KEY%"=="" (
    echo   [WARN] OPENALEX_API_KEY is NOT set -- OpenAlex will be SKIPPED.
    echo          Get a free key at https://openalex.org and re-run with:
    echo              set OPENALEX_API_KEY=...
) else (
    echo   OK    OPENALEX_API_KEY is set.
)
if "%RESEARCHER_EMAIL%"=="" (
    echo   [WARN] RESEARCHER_EMAIL is NOT set -- Crossref polite pool needs it.
) else (
    echo   OK    RESEARCHER_EMAIL = %RESEARCHER_EMAIL%
)
if "%SEMANTIC_SCHOLAR_API_KEY%"=="" (
    echo   note  SEMANTIC_SCHOLAR_API_KEY not set -- S2 falls back to
    echo         unauthenticated requests at 0.5 req/sec.
) else (
    echo   OK    SEMANTIC_SCHOLAR_API_KEY is set.
)

echo.

REM ---------------------------------------------------------------------------
REM  Make sure the output directory exists.  data\ is gitignored in this repo,
REM  so the example outputs never end up in commits.
REM ---------------------------------------------------------------------------
if not exist "%OUT_DIR%" mkdir "%OUT_DIR%"

echo Effective config:
echo   MAX_PER_SOURCE = %MAX_PER_SOURCE%
echo   YEAR_FROM      = %YEAR_FROM%
echo   YEAR_TO        = %YEAR_TO%
echo   SOURCES        = %SOURCES%
echo   OUT_DIR        = %OUT_DIR%
echo.

REM ===========================================================================
REM  Run 1  --  the SciMeto Discover UI "Load example" preset.
REM
REM  These three keywords are NOT the deepest or broadest spec; they are the
REM  ones the Discover UI ships specifically because they exercise every
REM  wildcard syntax in the engine's keyword expander:
REM
REM    replicat*                       trailing-stem expansion via STEM_DICT
REM    pre-?registered                 optional preceding char (zero or one)
REM    (close|high-powered) replication  alternation group
REM
REM  Together they expand to roughly 9 phrase variants which the engine
REM  combines into one OR-bundled query per source.
REM ===========================================================================
echo ===============================================================================
echo  Run 1  ::  "Load example"  (Discover UI's wildcard preset)
echo  Keywords:  replicat*    pre-?registered    (close^|high-powered) replication
echo ===============================================================================
echo.

python -m search.engine.cli ^
    --keywords "replicat*,pre-?registered,(close|high-powered) replication" ^
    --sources %SOURCES% ^
    --max-per-source %MAX_PER_SOURCE% ^
    --year-from %YEAR_FROM% --year-to %YEAR_TO% ^
    --out "%OUT_DIR%\example_load.csv"
if errorlevel 1 (
    echo [ERROR] Run 1 failed.
    popd
    exit /b 3
)

REM Show what we got back. -1 because line one of the CSV is the header.
for /f %%a in ('type "%OUT_DIR%\example_load.csv" ^| find /v /c ""') do set N1=%%a
set /a N1-=1
echo.
echo Run 1 wrote %N1% rows to %OUT_DIR%\example_load.csv
echo.

REM ===========================================================================
REM  Run 2  --  the Discover UI placeholder text.
REM
REM    replicat*
REM    direct replication
REM    failed to replicate
REM    (close|high-powered|preregistered) replication
REM
REM  Mostly the same wildcard surface as Run 1, but two extra literal phrases
REM  ("direct replication", "failed to replicate") that significantly raise
REM  recall on titles/abstracts that don't carry the wildcard stems.
REM ===========================================================================
echo ===============================================================================
echo  Run 2  ::  "Placeholder"  (Discover UI's textarea hint)
echo  Keywords:  replicat*  direct replication  failed to replicate
echo             (close^|high-powered^|preregistered) replication
echo ===============================================================================
echo.

python -m search.engine.cli ^
    --keywords "replicat*,direct replication,failed to replicate,(close|high-powered|preregistered) replication" ^
    --sources %SOURCES% ^
    --max-per-source %MAX_PER_SOURCE% ^
    --year-from %YEAR_FROM% --year-to %YEAR_TO% ^
    --out "%OUT_DIR%\example_placeholder.csv"
if errorlevel 1 (
    echo [ERROR] Run 2 failed.
    popd
    exit /b 4
)
for /f %%a in ('type "%OUT_DIR%\example_placeholder.csv" ^| find /v /c ""') do set N2=%%a
set /a N2-=1
echo.
echo Run 2 wrote %N2% rows to %OUT_DIR%\example_placeholder.csv
echo.

REM ===========================================================================
REM  Run 3  --  custom alternation showing the engine handles long lists
REM             without blowing past OpenAlex's URL-length budget.
REM
REM  All these phrases get OR-bundled into ONE OpenAlex query.  The full
REM  YAML spec contains 17 keyword IDs and ~84 phrase variants total; the
REM  engine still issues only one paginated search per source.
REM ===========================================================================
echo ===============================================================================
echo  Run 3  ::  "Custom"  (long alternation list, single OR-bundle call)
echo ===============================================================================
echo.

python -m search.engine.cli ^
    --keywords "(direct|conceptual|preregistered|registered|close|high-powered|systematic|many-labs|exact|near|approximate|external|independent|operational) replication" ^
    --sources %SOURCES% ^
    --max-per-source %MAX_PER_SOURCE% ^
    --year-from %YEAR_FROM% --year-to %YEAR_TO% ^
    --out "%OUT_DIR%\example_custom.csv"
if errorlevel 1 (
    echo [ERROR] Run 3 failed.
    popd
    exit /b 5
)
for /f %%a in ('type "%OUT_DIR%\example_custom.csv" ^| find /v /c ""') do set N3=%%a
set /a N3-=1
echo.
echo Run 3 wrote %N3% rows to %OUT_DIR%\example_custom.csv
echo.

REM ===========================================================================
REM  Run 4  --  spec-only.  No --keywords flag means "use only the YAML
REM             spec's 17 keyword IDs".  Closest analogue to a real
REM             production run, just capped to %MAX_PER_SOURCE% rows.
REM ===========================================================================
echo ===============================================================================
echo  Run 4  ::  "Spec only"  (no extra wildcards, just search\spec\search-keywords.yaml)
echo ===============================================================================
echo.

python -m search.engine.cli ^
    --sources %SOURCES% ^
    --max-per-source %MAX_PER_SOURCE% ^
    --year-from %YEAR_FROM% --year-to %YEAR_TO% ^
    --out "%OUT_DIR%\example_spec.csv"
if errorlevel 1 (
    echo [ERROR] Run 4 failed.
    popd
    exit /b 6
)
for /f %%a in ('type "%OUT_DIR%\example_spec.csv" ^| find /v /c ""') do set N4=%%a
set /a N4-=1
echo.
echo Run 4 wrote %N4% rows to %OUT_DIR%\example_spec.csv
echo.

REM ===========================================================================
REM  Stage 2 (filter)  --  optional, only fires when filter\rule_filter.py is
REM  implemented.  On feature/search the file is still a stub that raises
REM  NotImplementedError, so we detect that and print a friendly skip message
REM  instead of failing the whole walkthrough.
REM ===========================================================================
echo ===============================================================================
echo  Stage 2  ::  filter (rule + LLM)
echo ===============================================================================
echo.

REM Stage the engine output as candidates.csv so run_filter can consume it.
copy /Y "%OUT_DIR%\example_load.csv" "data\candidates.csv" >nul

python -c "from filter.rule_filter import apply_rule_filter; import inspect; src=inspect.getsource(apply_rule_filter); raise SystemExit(0 if 'NotImplementedError' not in src else 9)" >nul 2>&1
if errorlevel 9 (
    echo   note  filter\rule_filter.py is still the stub on this branch.
    echo         Switch to feature/filter to run Stage 2:
    echo             git fetch  ^&^&  git checkout feature/filter
    echo             python -m filter.run_filter
    goto :stage2_done
)
if errorlevel 1 (
    echo   [WARN] could not introspect filter.rule_filter -- skipping Stage 2.
    goto :stage2_done
)

python -m filter.run_filter
if errorlevel 1 (
    echo [WARN] filter step failed -- continuing anyway.
)

:stage2_done
echo.

REM ===========================================================================
REM  Summary
REM ===========================================================================
echo ===============================================================================
echo  Summary
echo ===============================================================================
echo   Run 1 (load example)     %N1% rows  --  %OUT_DIR%\example_load.csv
echo   Run 2 (placeholder)      %N2% rows  --  %OUT_DIR%\example_placeholder.csv
echo   Run 3 (custom alt list)  %N3% rows  --  %OUT_DIR%\example_custom.csv
echo   Run 4 (spec only)        %N4% rows  --  %OUT_DIR%\example_spec.csv
echo.
echo  Next steps:
echo    -  Open the CSVs in Excel; rows are in the canonical CANDIDATES_COLS schema.
echo    -  For Stage 2 (filter), check out feature/filter and run:
echo           python -m filter.run_filter
echo    -  For the full pipeline plus the web UI, check feature/filter's CLAUDE.md
echo       (search/filter/extract/validate views all live there).
echo.
echo  Engine internals are documented at docs\scimeto_engine_port.md.
echo ===============================================================================

popd
endlocal
exit /b 0
