# Setup Guide — FLoRA Extractor New Repository

**For the repository lead (Rohan) to follow before the hackathon starts.**  
Complete all steps before teams begin working.

---

## Step 1 — Create the GitHub Repository

1. Go to GitHub → New repository
2. Name: `flora-extractor` (or `FLoRA-extractor`)
3. Visibility: **Public** (for open-source validation UI goal)
4. Initialize with: **README** ✓, **.gitignore (Python)** ✓, **License: MIT** ✓
5. Clone locally:
   ```bash
   git clone https://github.com/YOUR_ORG/flora-extractor.git
   cd flora-extractor
   ```

---

## Step 2 — Create the Folder Structure

Run this from the root of the new repo:

```bash
mkdir -p search filter extract validate/routes validate/templates shared misc data/samples tests cache

# Create __init__.py for Python packages
touch search/__init__.py filter/__init__.py extract/__init__.py
touch validate/__init__.py validate/routes/__init__.py
touch shared/__init__.py tests/__init__.py

# Create placeholder orchestrators so teams have something to open
touch search/run_search.py filter/run_filter.py extract/run_extract.py
touch validate/app.py validate/import_csv.py validate/models.py validate/state.py
touch extract/link_original.py extract/multi_original.py extract/code_outcome.py
touch search/openalex_search.py search/external_lists.py search/deduplicate.py
touch filter/rule_filter.py filter/llm_filter.py

# Shared modules (to be filled in Step 4)
touch shared/openalex_client.py shared/llm_client.py shared/pdf_sources.py
touch shared/grobid.py shared/disambiguation.py shared/utils.py
touch shared/config.py shared/schema.py shared/cache.py

# Misc reference files
touch misc/openalex_api_example.py misc/gemini_api_example.py
```

---

## Step 3 — Copy the Root MD Files

Copy the following files from `docs/new_repo/` in `flora_search_approaches` to the **root** of the new repo:

| Source (flora_search_approaches/docs/new_repo/) | Destination (flora-extractor/) |
|---|---|
| `CLAUDE.md` | `CLAUDE.md` |
| `RULEBOOK.md` | `RULEBOOK.md` |
| `README.md` | `README.md` |
| `.env.example` | `.env.example` |
| `requirements.txt` | `requirements.txt` |
| `shared/schema.py` | `shared/schema.py` |

---

## Step 4 — Port Code from OpenAlexLLM

Copy and rename the following files from `flora_search_approaches/OpenAlexLLM/`:

```
SOURCE (OpenAlexLLM/)              DESTINATION (flora-extractor/)
─────────────────────────────────────────────────────────────────
lib/openalex.py               →    shared/openalex_client.py
lib/llm.py                    →    shared/llm_client.py
lib/pdf_sources.py            →    shared/pdf_sources.py
lib/grobid.py                 →    shared/grobid.py
lib/disambiguation.py         →    shared/disambiguation.py
lib/utils.py                  →    shared/utils.py
lib/config.py                 →    shared/config.py        ← UPDATE PATHS (see Step 5)
lib/pipeline.py               →    extract/link_original.py
lib/multi_original.py         →    extract/multi_original.py  ← NOTE: needs improvement
state.py                      →    validate/state.py
routes/batch.py               →    validate/routes/batch.py
routes/multi_originals.py     →    validate/routes/multi_originals.py
routes/input_bp.py            →    validate/routes/input.py
routes/disambiguation.py      →    validate/routes/disambiguation.py
```

Also copy the `templates/` folder:
```
templates/   →   validate/templates/
```

---

## Step 5 — Update `shared/config.py`

After copying `lib/config.py` to `shared/config.py`, update the path constants to match the new repo structure. Change any paths that reference `OpenAlexLLM/data/` to `data/` and any `OpenAlexLLM/cache/` to `cache/`.

Key paths to verify:
```python
DATA_DIR         = Path(__file__).parent.parent / "data"
CACHE_DIR        = Path(__file__).parent.parent / "cache"
PDF_CACHE_DIR    = CACHE_DIR / "pdfs"
LLM_CACHE_DIR    = CACHE_DIR / "llm"
OA_CACHE_DIR     = CACHE_DIR / "openalex"
GROBID_CACHE_DIR = CACHE_DIR / "grobid"
```

---

## Step 6 — Fix Import Paths in Ported Files

After copying, do a find-and-replace in each ported file:

| Old import | New import |
|---|---|
| `from .config import` | `from shared.config import` (in extract/ files) |
| `from .utils import` | `from shared.utils import` |
| `from .openalex import` | `from shared.openalex_client import` |
| `from .llm import` | `from shared.llm_client import` |
| `from .pdf_sources import` | `from shared.pdf_sources import` |
| `from .grobid import` | `from shared.grobid import` |
| `from .disambiguation import` | `from shared.disambiguation import` |
| `import state` | `import validate.state as state` (in validate/ files) |
| `from lib.` | `from shared.` |

---

## Step 7 — Create Sample Data Files

Create small (10–20 row) sample CSVs so teams can work independently.

### `misc/sample_candidates.csv`
Copy 10-20 rows from `OpenAlexLLM/data/openalex_candidates.csv` and keep only these columns:
```
doi_r, title_r, abstract_r, year_r, authors_r, journal_r, url_r, openalex_id_r, source
```
Set `source = "openalex"` for all rows.

### `misc/sample_filtered.csv`
Take `misc/sample_candidates.csv` and add:
```
filter_status=replication, filter_method=rule_based, filter_evidence="replication of",
filter_confidence=1.0, is_replication=True, is_reproduction=False,
original_match_type=single_original, original_match_confidence=0.95
```
for all rows (simplified for testing).

### `misc/sample_extracted.csv`
Copy 10-20 rows from `OpenAlexLLM/data/multiple_match_resolved.csv` and map columns to the new schema.

---

## Step 8 — Set Up Environment

```bash
# In the new repo root:
cp .env.example .env
# Edit .env and add your API keys

pip install -r requirements.txt
```

---

## Step 9 — Add `.gitignore` Entries

Ensure these are in `.gitignore`:
```
# Data files (too large / sensitive)
data/*.csv
data/*.xlsx
!data/samples/

# Caches
cache/

# Environment
.env

# Python
__pycache__/
*.pyc
*.pyo
.pytest_cache/
```

---

## Step 10 — Create Branches and Protect Main

```bash
# Create integration branch
git checkout -b dev
git push origin dev

# Create team branches
git checkout -b feature/search && git push origin feature/search
git checkout -b feature/filter && git push origin feature/filter
git checkout -b feature/extract && git push origin feature/extract
git checkout -b feature/validate && git push origin feature/validate

git checkout dev  # return to dev
```

On GitHub → Settings → Branches → Add branch protection rule for `main`:
- ✓ Require pull request reviews (1 reviewer)
- ✓ Require status checks to pass
- ✓ Do not allow force pushes

---

## Step 11 — Create GitHub Issues for Each Team

Create one issue per team as their task tracker:

**Issue 1: [Search] Stage 1 — Identification Pipeline**
- Assigned to: Team Search
- Branch: `feature/search`
- Deliverable: `search/run_search.py` produces valid `data/candidates.csv`
- Test: runs on sample data in `misc/`

**Issue 2: [Filter] Stage 2 — Filter Pipeline**
- Assigned to: Team Filter
- Branch: `feature/filter`
- Deliverable: `filter/run_filter.py` produces valid `data/filtered.csv`
- Test: runs on `misc/sample_candidates.csv`

**Issue 3: [Extract] Stage 3 — Extraction Pipeline**
- Assigned to: Team Extract
- Branch: `feature/extract`
- Tasks:
  - Port shared/ modules and fix import paths
  - Write `extract/run_extract.py` orchestrator
  - Improve `extract/multi_original.py` (known flaws in detection)
  - Write `extract/code_outcome.py`
- Deliverable: `extract/run_extract.py` produces valid `data/extracted.csv`

**Issue 4: [Validate] Stage 4 — Validation Web App**
- Assigned to: Team Validate
- Branch: `feature/validate`
- Tasks:
  - Write `validate/models.py` (SQLite schema)
  - Write `validate/import_csv.py`
  - Write `validate/routes/review.py` (voting queue)
  - Port batch/dashboard/export routes
- Deliverable: Flask app runs on port 5001 with voting UI

---

## Step 12 — Send Teams Their Starting Instructions

Each team gets:
1. Link to the GitHub repo
2. Their branch name
3. Their issue number
4. Instructions: "Read `CLAUDE.md` and `RULEBOOK.md` first. Use `misc/sample_*.csv` to test. Open a PR to `dev` when done."

---

## Day-by-Day Merge Schedule

| Time | Action |
|------|--------|
| Day 1 AM | All teams clone repo, read CLAUDE.md + RULEBOOK.md, set up environment |
| Day 1 PM | All teams working on their feature branches using sample data |
| Day 2 AM | Team Extract finishes porting `shared/` — push to feature/extract |
| Day 2 PM | feature/search → PR → dev (other teams can now pull real candidates) |
| Day 3 AM | feature/filter → PR → dev (Team Extract switches to real filtered.csv) |
| Day 3 AM | feature/extract → PR → dev (Team Validate loads real extracted.csv) |
| Day 3 PM | feature/validate → PR → dev → final demo |

---

## Verifying Everything Works

After Steps 1-11, run this quick check:

```bash
# In new repo root:
python -c "from shared.utils import clean_doi; print(clean_doi('https://doi.org/10.1037/abc123'))"
# Should print: 10.1037/abc123

python -c "from shared.openalex_client import extract_author_year_patterns; print(extract_author_year_patterns('Smith (2020) replicated Jones (2016)'))"
# Should print: list with two pattern dicts

python -c "import pandas as pd; from shared.schema import CANDIDATES_COLS; df = pd.read_csv('misc/sample_candidates.csv'); assert all(c in df.columns for c in CANDIDATES_COLS); print('Schema OK')"
```

If all three pass, the repo is ready for teams.
