# Stage 3 — Extraction Pipeline: Implementation Notes

This document covers implementation decisions, bug fixes, and behavioural rules added to
Stage 3 (`extract/`) and supporting modules during the current development cycle.
It is a living reference for developers — update it when pipeline behaviour changes.

---

## 1. Single-Original Linking Pipeline (`extract/link_original.py`)

### Stage ordering

```
Stage 3:   _resolve_rule_based()      ← rule-based (citation-context + same-author/year)
Stage 4:   Abstract-level LLM         ← when abstract exists and has any signal
Stage 5:   PDF acquisition
Stage 6:   GROBID reference extraction
Stage 7:   Full-text LLM              ← guarded: refuses to run with no context
```

### Stage 3 — Unified rule-based resolver (`_resolve_rule_based`)

Replaces the old two-function pair (`_resolve_by_citation_context` + `resolve_same_author_year`).
A single function handles all rule-based resolution before any LLM call.

**Path A — journal hint present in abstract citation:**

Parses `(Author, Year, Journal)` patterns from the HTML-decoded abstract using `_CITATION_RE`.
Scores each OpenAlex candidate:

| Signal | Points |
|---|---|
| Author surname match | +2.0 |
| Exact year match | +2.0 |
| Year ±1 | +1.0 |
| Journal Jaccard ≥ 0.60 | +3.0 |
| Journal Jaccard ≥ 0.30 | +1.5 |
| Title Jaccard vs abstract (capped) | +≤1.0 |

Resolves when `best ≥ 4.0 AND gap over #2 ≥ 2.0`. The strict gap threshold requires
the journal to contribute 3 points — it won't fire on title Jaccard alone.

Journal names for candidates are fetched individually from OpenAlex (`_fetch_journal_cached`)
because the bulk `referenced_works` endpoint omits `primary_location`. Cached under
`cache/openalex/journal_<hash>.json`.

**Path B — no journal hint, same-author/year cluster:**

Fires when all candidates share one first-author surname and one year. Uses title Jaccard
against `abstract + replication title`. Threshold: `best > 0.05 AND best ≥ second × 1.5`.

**Single candidate:**

Resolved immediately unless it looks like an umbrella paper (`is_umbrella_paper()`).

**Why ordering matters — `10.1111/cdev.13309` bug:**

The old pipeline ran same-author/year (Path B) before citation-context (Path A). Both
Sutherland 2012 papers were in candidates. "Just pretending can be really learning"
(Developmental Psychology) scored higher on title Jaccard because "pretend/pretense"
appears in the replication title — so the wrong paper was returned, and Path A never ran.

Fix: `_resolve_rule_based` always tries Path A first. Path B only fires when Path A doesn't
resolve, which happens when no journal hint is present in any citation.

**`html.unescape()` requirement:**

Crossref abstracts contain JATS XML entities (`&amp;` → `&`). Without unescaping,
`(Sutherland & Friedman, 2012)` is stored as `(Sutherland &amp; Friedman, 2012)` and
`_CITATION_RE` fails to match both surnames. Applied at entry to `_resolve_rule_based`.

---

### Stage 4 — Abstract-level LLM

**Previous condition (too strict):**

```python
if len(distinct_pairs) >= 2 and candidates:
```

This silently skipped papers where the abstract named the original clearly but had only
1 citation pattern or 0 OpenAlex candidates. Example: `10.1037/cou0000110` abstract says
"We replicated Son, Ellis, and Yoo (2013)" — 1 pattern, 0 candidates → Stage 4 never fired
→ fell to title-only LLM → resolved to the wrong paper.

**Fixed condition:**

```python
if abstract_r and (distinct_pairs or candidates):
```

Stage 4 fires whenever the abstract is non-empty and contains any citation pattern OR any
OpenAlex candidate exists. The abstract LLM is cheap (no PDF download); there is no good
reason to skip it when context is available.

---

### Stage 7 — Empty-context guard

The LLM must not be called when it has nothing to reason from. Title-only prompts produce
hallucinations that look confident but are wrong. The guard checks before Stage 7:

```python
_has_context = (
    abstract_r
    or candidates
    or sections.get("intro")
    or sections.get("references")
)
if not _has_context:
    # write target_pending with resolution_method="no_context"
```

`no_context` maps to `target_pending` in `_METHOD_MAP` so reviewers see it in the Extract
tab and know this DOI needs manual attention, not a wrong automated result.

**All three problematic DOIs (`10.1007/s10857-019-09443-2`, `10.1037/cou0000110`,
`10.1080/16506073.2015.1015163`) hit this path** because their abstracts were missing from
`filtered.csv`. Re-running `load_doi_list.py` will populate the abstracts (via the OpenAlex
fallback), after which Stage 4 will fire and resolve correctly for at least
`10.1037/cou0000110` (whose abstract explicitly names the original).

---

## 2. Missing Abstracts — Root Cause and Fix

`filtered.csv` rows with empty `abstract_r` are the primary failure mode for Stage 3.
Without an abstract:

- `find_all_candidates()` has no author-year patterns → returns 0 candidates
- `_resolve_rule_based` returns not-resolved immediately
- Stage 4 doesn't fire (no abstract)
- Stage 7 gets called blind → hallucinates

**Fix:** Re-run `python load_doi_list.py` after the OpenAlex fallback was added. The
script now tries Crossref first, then falls back to OpenAlex `abstract_inverted_index`:

```python
if not meta.get("abstract"):
    oa_abstract = _openalex_abstract(doi)   # reconstructs from inverted index
    if oa_abstract:
        meta["abstract"] = oa_abstract
```

After regenerating `filtered.csv`, clear the LLM and candidates caches for affected DOIs
using `clear_pipeline_caches(doi_r)` and re-run Stage 3.

---

## 3. LLM Cache Does Not Include Model Name

The cache key for `identify_original_with_llm` is `cache_key(doi_r)` — it does not include
the model name. This means:

- Switching from Gemini to Qwen (by setting `OPENROUTER_API_KEY`) **does not invalidate
  existing cached results**. Previously-resolved DOIs return the cached Gemini answer.
- A "blank LLM prompt" in the Extract tab UI usually means the result came from a cache
  written before the `llm_prompt` field was added to the output schema. The field defaults
  to `""` via `cached.setdefault("llm_prompt", "")`.

**To force a fresh call with the new model**, manually delete the cache file:

```python
from extract.link_original import clear_pipeline_caches
clear_pipeline_caches("10.1037/cou0000110")   # deletes llm_<hash>.json
```

Or run with `force=True` from the web app re-run button, which calls `clear_pipeline_caches`
before running.

There is currently no automatic cache-busting on model change. This is a known gap — adding
the model name to the cache key would bust all caches on every model update, which may not
be desirable during testing.

---

## 4. Multiple-Original Pipeline (`extract/multi_original.py`, `extract/run_extract.py`)

### `force_multi` — when it fires and what it does

`force_multi=True` is passed to `identify_all_originals_with_llm()` only when the
**rule-based classifier** (`_rule_classify_multi_original`) fires. It does two things:

1. Bypasses the stale `multi_<hash>.json` LLM cache (prevents `is_false_positive:true`
   from a prior run overriding the rule decision).
2. Injects `"CONFIRMED MULTI-TARGET"` directive into the prompt, instructing the LLM
   not to return a false positive.

Papers classified as `multiple_original` **only by the LLM** classifier have
`rule_fired=False` → `force_multi=False`. For a fresh run these work correctly, but
a stale bad cache is not busted. The current rule patterns catch all known real-world
multi-target replication papers (Many Labs, RRR, PSA, "replications of N ≥ 3 studies").

### Rule patterns for multi-original classification

| Pattern type | Example match |
|---|---|
| Title: "many labs" | "Many Labs 2", "ManyLabs 3" |
| Title: "registered replication report" | "A Registered Replication Report of…" |
| Abstract: "replications of N" (N≥3) | "We conducted replications of 28 findings" |
| Abstract: "replicated N original/classic studies" | "We replicated 10 classic findings" |
| Abstract: "N independent/classic studies" | "28 distinct findings were targeted" |

### Why Many Labs 2 started working

Before the fix, `classify_match_type()` checked the cache before running rules. A prior
LLM run had returned `is_false_positive: true` and written it to
`cache/llm/multi_<hash>.json`. On re-run this stale cache was returned immediately,
skipping the LLM entirely.

The fix: rules run **before** the cache check in `classify_match_type()`. When a rule
fires, `rule_fired=True` is set and the stale multi-cache is bypassed via `force_multi`.

### Fallback when LLM returns zero originals for a rule-confirmed paper

```text
rule_fired=True  AND originals=[] → write target_pending row (NOT single_original)
rule_fired=False AND originals=[] → fall through to single-original pipeline
```

### `force_multi` only fires for rule-confirmed papers

Papers that the LLM classifies as `multiple_original` but don't match any rule pattern get
`force_multi=False`. A stale `is_false_positive: true` cache for those is not busted.
This is a known gap — the rule patterns are conservative by design, and expanding them
risks misclassifying borderline cases as multi-target.

---

## 5. LLM Provider Routing (`shared/llm_client.py`)

### Provider chain for `identify_original_with_llm`

```text
if OPENROUTER_API_KEY is set:
    try OpenRouter (default: qwen/qwen3.5-35b-a3b)
    → on success, return
if not resolved yet:
    try Gemini (GEMINI_HEAVY_MODEL)
    → on success, return
    try OpenAI (OPENAI_MODEL) as last fallback
```

Same chain for `identify_all_originals_with_llm`. `resolution_method` in output shows
`llm_openrouter`, `llm_gemini`, or `llm_openai` — visible in the Extract tab.

### Model selection per task

| Task | Config var | Default |
| --- | --- | --- |
| `classify_match_type` | `GEMINI_LIGHT_MODEL` | `gemini-2.5-flash-lite` |
| `code_outcome` | `GEMINI_LIGHT_MODEL` | `gemini-2.5-flash-lite` |
| `identify_original_with_llm` | `OPENROUTER_HEAVY_MODEL` / `GEMINI_HEAVY_MODEL` | `qwen/qwen3.5-35b-a3b` / `gemini-3-flash-preview` |

### Model name tracking (`call_llm` returns 3-tuple)

`call_llm` was originally a 2-tuple return `(result, error)` — callers had no way to know
which provider actually answered. It now returns a 3-tuple:

```python
result, model_used, error = call_llm(prompt, gemini_model=GEMINI_LIGHT_MODEL)
# model_used: exact model string that answered, or "" if all providers failed
# e.g. "qwen/qwen3.5-35b-a3b", "gemini-2.5-flash-lite", "gpt-4o-mini"
```

The provider chain and the returned `model_used` value:

| Provider | `model_used` value |
| --- | --- |
| OpenRouter (when `OPENROUTER_API_KEY` set) | `OPENROUTER_HEAVY_MODEL` config value |
| Gemini | `gemini_model` arg passed to `call_llm` |
| OpenAI | `OPENAI_MODEL` config value |

**What gets stored where:**

- `extract/run_extract.py` `_llm_classify_match_type` → writes `classify_llm_model` into
  `cache/llm/match_type_<hash>.json`. Also writes the LLM's `reasoning` field (previously
  it was discarded even though the detail panel was reading it).
- `extract/code_outcome.py` `_llm_outcome` → writes `llm_model` into
  `cache/llm/outcome_<hash>.json`.
- `extract/link_original.py` `identify_original_with_llm` → has always written `llm_model`
  into `cache/llm/llm_<hash>.json` (unchanged).

**Effect on the UI:** the LLM MODEL column in the Extract table and the model tags in the
detail panel now show the real model name for new pipeline runs. Rows cached before this
change still show `—`; use the re-run button to refresh individual rows.

**Cache-busting caveat:** the cache *key* still does not include the model name (see Known
Gaps). Recording the model in the cache value is for observability only — it does not
cause a fresh call when the configured model changes.

### Anti-hallucination guard for DOIs

When the LLM selects a candidate number, the candidate's verified OpenAlex DOI is
used in preference to any DOI the LLM may have written. The LLM is instructed to
leave `selected_doi` empty when choosing by candidate number.

---

## 6. `pair_id` — Replication-Pair Hash

Every extracted row carries a `pair_id`: MD5 of `doi_r + "|" + doi_o` (after `clean_doi()`),
32-character hex. The Extract tab shows the first 3 characters; full hash in tooltip.

```python
# shared/schema.py
def make_pair_id(doi_r: str, doi_o: str) -> str:
    return hashlib.md5(f"{doi_r}|{doi_o}".encode()).hexdigest()
```

When `doi_o` is empty (unresolved), `pair_id` uses empty string as second component.

---

## 7. `load_doi_list.py` — Abstract Fallback via OpenAlex

When Crossref returns no abstract, falls back to OpenAlex `abstract_inverted_index`:

```python
pos_word = {pos: word for word, positions in inv.items() for pos in positions}
abstract = " ".join(pos_word[i] for i in sorted(pos_word))
```

This is critical: without an abstract, the Stage 3 citation-context parser has nothing
to work from, and the Stage 4 abstract LLM is also skipped. Missing abstracts are the
primary cause of wrong LLM resolutions.

---

## 8. `lookup/probe_doi.py` — Interactive DOI Probe Tool

Stand-alone terminal tool for testing the rule-based resolver against a single DOI.

```bash
python lookup/probe_doi.py 10.1111/cdev.13309
```

Logs to `lookup/logs/<doi_slug>_<timestamp>.txt`.

**Note:** `probe_doi.py` uses its own standalone scoring implementation. If `_resolve_rule_based`
in `link_original.py` is updated, the probe tool must be kept in sync manually.

---

## 9. UI (`validate/templates/extract.html`)

### `linkResolved` condition

Multi-original LLM results sometimes resolve a title but not a DOI. The old check
`d.doi_o && ...` showed "Not yet resolved" in those cases. Fixed:

```javascript
const linkResolved = (d.doi_o || d.title_o)
    && d.link_method !== 'target_pending'
    && d.link_method !== 'api_error';
```

### `pair_id` column

First visible column after row number. 3-char monospace label, full hash in tooltip.

### LLM MODEL column (`link_llm_model`)

The Extract table has a dedicated **LLM MODEL** column showing `link_llm_model` from
`extracted.csv` — the model used for DOI resolution. The column filter accepts free-text
so reviewers can filter to rows resolved by a specific model.

The detail panel shows model tags for all three LLM steps:

| Panel section | Field read | Cache file |
| --- | --- | --- |
| STAGE 3 ROUTING | `classify_llm_model` | `match_type_<hash>.json` |
| RESOLVED — ORIGINAL STUDY | `llm_model` | `llm_<hash>.json` |
| OUTCOME | `outcome_llm_model` | `outcome_<hash>.json` |

### Model comparison run panel (`/api/extract/run-doi`)

The **Run selected with model** action bar lets reviewers re-run any row through all
three Stage 3 LLM steps with a chosen model without writing to `extracted.csv`:

```text
POST /api/extract/run-doi
Body: { "doi": "10.xxx/yyy", "model": "gemini-2.5-pro-preview-06-05" }

Response:
{
  "classify": { "prompt": "...", "result": {...}, "error": "" },
  "link":     { "prompt": "...", "result": {...}, "error": "", "n_candidates": 12, "n_refs": 38 },
  "outcome":  { "prompt": "...", "result": {...}, "error": "" }
}
```

The result panel (dark card labelled **MODEL TEST**) appears inside the row's expanded
detail view. Multiple model runs for the same row stack newest-first. Each step has a
collapsible "Prompt / raw response" section for debugging.

Model routing in `_call_model` (used only for this comparison endpoint — not the live
pipeline): model name prefix determines provider: `gemini-*` → Gemini, `gpt-*`/`o1`/`o3`/`o4`
→ OpenAI, everything else → OpenRouter.

---

## 10. Known Gaps and Future Work

- **Cache key does not include model name** — switching providers doesn't invalidate
  cached results. The model name that answered is now *recorded* inside each cache file
  (as `classify_llm_model`, `llm_model`, `outcome_llm_model`) so the UI can show it, but
  this is observability only. Manual `clear_pipeline_caches(doi_r)` or the re-run button
  (`force=True`) is still needed to get a fresh call after switching models.

- **`probe_doi.py` vs pipeline discrepancy** — two separate scoring implementations.
  Changes to `_resolve_rule_based` in `link_original.py` must be mirrored in `probe_doi.py`.

- **`force_multi` only busts cache for rule-confirmed papers** — LLM-classified multi-original
  papers get `force_multi=False`. Stale bad caches are not busted for those.

- **Threshold tuning not validated** — `score ≥ 4.0, gap ≥ 2.0` thresholds for Path A and
  `best > 0.05, best ≥ second × 1.5` for Path B have not been evaluated against a gold standard.
  Run against `data/flora_selected.csv` before a production run.

- **`citation_context_match` not a distinct schema value** — maps to `author_year_match` in
  `_METHOD_MAP`. Consider adding as a distinct `link_method` so reviewers can filter
  rule-resolved rows from Jaccard-resolved rows in the Extract tab.
