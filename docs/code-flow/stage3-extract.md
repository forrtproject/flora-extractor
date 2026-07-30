# Stage 3: Extract — Code Flow

**Entry point:** `python -m extract.run_extract`

## What it does

For each row of `filtered.csv` that Stage 2 accepted, Stage 3 answers two questions:

1. Which original study does this paper target (`doi_o`, `title_o`, `link_method`)?
2. What was the result (`outcome`, `outcome_phrase`)?

Rows are appended to `data/extracted.csv` one at a time, so the monitoring app can be
opened while a run is still going. Every row passes DOI verification and
`oa_work_id_*` stamping on the way out, and `extract/sanity_check.py` runs at the end
of every invocation — on normal completion and on Ctrl-C.

The whole stage is organised around spending the cheap calls first. The classification
screen discards 58% of the rows that reach it, the resolution ladder returns at the
first rung that resolves, and the outcome LLM — the most expensive call, because it can
escalate to 8,000 characters of full text — runs only on a row that already has a
confirmed original.

## Per-row flow

```
run_extract.py
    │
    ├── load filtered.csv, apply --year / --source / --doi-r / --predicted-outcome filters
    ├── skip DOIs already in FLoRA (entry sheet + flora.csv) unless --no-skip-flora-validated
    ├── skip false_positive rows (Stage 2 already rejected them)
    ├── --resume: write every already-resolved row out first, re-run only target_pending
    ├── --extracted-test: write to extracted-test.csv, skip DOIs resolved in extracted.csv
    │
    └── for each remaining row:
            │
            ├── FRONT DOOR — classify_replication(doi_r, title_r, abstract_r)
            │       two models vote on "is this a replication or reproduction at all?"
            │       ├── agreed "no", both high confidence  → not_a_replication, row done
            │       ├── models disagree                    → screen_disagreement, row done
            │       ├── one vote answered                  → target_pending, row done
            │       ├── no votes answered                  → api_error, row done
            │       └── anything else                      → continue
            │
            ├── classify_match_type(row) → original_match_type
            │
            ├── if multiple_original:
            │       run_multi_original_for_doi(doi_r) → N originals
            │       → N rows (original_rank = 1, 2, 3 …), each guarded and written
            │
            └── else (single_original / multiple_match) — _resolve_and_code():
                    run_for_doi(doi_r, classification=screen)   ← the ladder, below
                    _merge_row()  → build the output row
                    _guard_original_link()  → reject self-links, recover a missing doi_o
                    --resolved-only         → drop the row here if it has no link
                    _outcome_without_coding() → outcome gate (below)
                        └── if the row is codeable: parse cache → extract_outcome()
```

A row that the front door ends never pays for the match-type call, the ladder, PDF
acquisition or outcome coding. `--rescreen` reopens exactly the rows a previous run set
aside on the screen's own verdict, wherever a previous run's rows are being read
(`--resume`, or the production CSV that `--extracted-test` skips against), so a changed
voter pair or prompt decides them again. Every other resolved row is carried forward
untouched, and a multi-original paper is reopened as a unit.

## The front door (`shared/llm_client.classify_replication`)

Two providers vote, and the pipeline acts only when they agree:

| Voter | Provider | Model |
| ----- | -------- | ----- |
| 1 | Gemini | `GEMINI_LIGHT_MODEL` |
| 2 | OpenRouter | `SCREEN_VOTER2_MODEL` (default `mistralai/ministral-14b-2512`) |

Both keys are required — `run_extract` refuses to start without `GEMINI_API_KEY` and
`OPENROUTER_API_KEY` (unless `--no-llm`), because with one provider every row returns a
single vote, which is not a verdict. Voter 2 sits outside the Google lineage on
purpose: its errors overlap little with voter 1's.

`classification_confidence` is the **weaker** of the two votes, and is populated only
when the models agree — so `== "high"` means both voters were sure. Discarding a row
requires an agreed "no" at that bar.

A screen that did not get both votes is an API failure, not a verdict: it is returned
uncached (`llm_refscreen_partial` with one vote, `llm_refscreen_failed` with none) so a
re-run can decide the row once the provider is back. Rows set aside for review carry
each voter's label and confidence in `link_evidence` and both model names in
`link_llm_model` — "the models disagreed" alone is not something a reviewer can act on.

Cached at `cache/llm/classify_{key}.json`, keyed on the prompt version, **both** model
names and the abstract itself.

## Match-type classification (`classify_match_type`)

```
classify_match_type(row)
    │
    ├── deterministic rules on title + abstract (run BEFORE the cache, so a stale
    │   LLM result cannot override them):
    │       "Many Labs" / "Many Analysts" in the title            → multiple_original
    │       "replication of N studies/findings/papers", 3 ≤ N < 1900 → multiple_original
    │
    ├── fewer than 2 distinct author-year pairs in title + abstract
    │       → single_original, no LLM call (the only abstract evidence for several
    │         different targets is several different citations)
    │
    └── otherwise: find_all_candidates() → LLM (GEMINI_HEAVY_MODEL) chooses
            single_original | multiple_match | multiple_original
            OpenAlex failure or LLM failure → single_original at low confidence
```

Registered Replication Reports are deliberately not treated as multi-target: an RRR is
many labs replicating **one** original.

## Original-study resolution ladder (`link_original.run_for_doi`)

Cheapest first; the function returns at the first rung that resolves, so the PDF is a
last resort rather than the normal path. The front door's verdict is threaded in as the
`classification` argument, so the target pick reuses it instead of voting again (a
caller with no verdict — the batch tools — lets the screen vote there).

| # | Rung | Fires when | `link_method` on success |
| - | ---- | ---------- | ------------------------ |
| 1 | Base data | always — FLoRA sheet row + candidate pass-through fields | (not a resolver) |
| 2 | OpenAlex candidate re-query | always — builds the candidate pool from `referenced_works` | (not a resolver) |
| 2.5 | Title-pattern resolver | the title matches "A Replication of X" and one candidate matches that target by Jaccard (≥ 0.4, and ≥ 1.5× the runner-up) | `title_pattern_match` |
| 3 | Rule-based resolver | the abstract carries an author-year citation matching a candidate, or exactly one candidate came back | `citation_context_match`, `same_author_year_title_overlap`, `single_candidate_after_requery` |
| 4 | Abstract LLM | the abstract carries author-year patterns and there are candidates to choose from | `llm_cited_candidates` |
| 4.5 | Reference-list target pick | there are referenced works (OpenAlex, or OpenCitations as fallback) | `llm_references` |
| 4.6 | Pre-PDF title search | the screen agreed at high confidence that this is a replication and named a target it could not match to any reference | `llm_title_search` (**provisional**) |
| 5 | PDF acquisition + full-text LLM | everything above declined | `llm_fulltext`, `llm_title_search` (**provisional**) |

Rungs 2.5–4 all depend on `find_all_candidates()`, which returns `[]` unless the title
or abstract contains a parseable `(Author, Year)` citation. Many abstracts — clinical
and life-sciences ones especially — carry none, so for those papers the ladder starts
at 4.5.

**Rung 4.5** (`screen_references_with_llm`) shows the model the abstract plus the
paper's reference list and asks which numbered reference is the target. A reference is
accepted **only at `confidence == "high"`**; at medium or low the row escalates, because
a wrong original is worse than a slow one, and the prompt says explicitly that most
abstracts do not name their target and that declining is the expected answer. The pick
is cached separately from the classification (`cache/llm/reftarget_{key}.json`) — the
two halves are decided at different points in the pipeline, and the rendered reference
list is in the key, so a re-fetched list cannot replay a pick that pointed at a
different paper.

**Rungs 4.6 and 5** can both resolve a DOI by searching CrossRef/OpenAlex for a title
the model named, and both record `llm_title_search`. This is the one link method whose
answer is not chosen from a bounded candidate set — a paper that is not a replication
can be confidently linked to a landmark it merely cites — and a hand-check measured it
near 50% precision. It is therefore **not** in `RESOLVED_LINK_METHODS`:
`link_confidence` is forced to `low`, no outcome is coded, `csv_to_db` does not import
the row, and `sanity_check` moves it to `data/provisional_title_search.csv` for human
confirmation. Rung 4.6 is additionally gated on both voters having called the paper a
replication at high confidence.

If no PDF and no OpenAlex XML can be acquired, the row is written `target_pending`
rather than sent to the LLM with nothing to read — that is exactly how a confident,
fabricated `doi_o` gets produced.

### Guarding the link

`_guard_original_link()` runs on every produced row before it is written:

- a paper is never its own original (same DOI or identical title) → `target_pending`
- `doi_o` blank but `title_o` present → try CrossRef then OpenAlex title search
- still no DOI but the title is substantive and distinct → keep the row with
  `doi_o_verification = "no_doi"` (old papers, book chapters and working papers have
  genuine originals with no registered DOI)
- no DOI and no usable title → `target_pending`

`_verify_row()` then checks that `doi_o` actually points at the claimed paper (the
thresholds and the three correction tiers are in `shared/doi_verify.py`; see also
*DOI Verification* in `CLAUDE.md`), and `_fill_work_ids()` stamps
`oa_work_id_r` / `oa_work_id_o` afterwards, so the o-side ID always describes the DOI
finally written.

## Outcome coding

`_outcome_without_coding()` in `run_extract.py` is the single gate. A row whose
`link_method` is not in `RESOLVED_LINK_METHODS` has no confirmed original to code an
outcome against, so no outcome LLM runs:

| `link_method` | `outcome` written |
| ------------- | ----------------- |
| in `RESOLVED_LINK_METHODS` | coded by `extract_outcome()` |
| `not_a_replication` | `not_a_replication` — the screen's verdict *is* the outcome |
| `api_error` | `api_error` |
| `llm_title_search` | `cannot_be_determined` (provisional link, outcome deferred) |
| `screen_disagreement`, `target_pending`, `no_original_found` | `pending`, with the reason in `outcome_reasoning` |

The order per row is resolve → merge → guard → `--resolved-only` → outcome, so a row
the guard demotes or `--resolved-only` discards never reaches the outcome call.

```
extract_outcome(doi_r, abstract_r, fulltext, title_r, record_type=…)
    │
    ├── record_type == "reproduction" → straight to the LLM on the 3×3
    │   computation/robustness grid (the replication keyword patterns would code it
    │   in the wrong vocabulary)
    │
    ├── --no-llm → keyword scan of the title (high-confidence hits only) then the
    │   abstract; fulltext is never keyword-scanned, because an introduction's
    │   background prose about OTHER studies' outcomes misfires the patterns
    │
    └── otherwise → _llm_outcome():
            abstract pass (GEMINI_HEAVY_MODEL, OpenAI preferred on retry)
            └── returns cannot_be_determined, or there is no abstract, and parsed
                fulltext exists → second, fulltext-based call (8,000-char cap),
                disable with OUTCOME_FULLTEXT_ESCALATION=false
            the same call judges is_genuine_attempt; false → not_a_replication
```

Exhausting every provider yields `outcome = api_error`, never a verdict, and is not
cached — `cannot_be_determined` is a judgement about the paper, and recording one for a
quota outage would make the two indistinguishable.

Every "is this a genuine replication?" decision is seen by the LLM when one is
available: a bare keyword hit like "failed to replicate" can fire on background prose,
so short-circuiting on it would let non-replications through as coded replications.

The text handed to the outcome LLM is the best-scoring entry in the parse cache
(`_best_fulltext_from_cache`), preferring `raw_text` and falling back to
`abstract + intro`. An all-empty parse cache counts as a miss — it is what a PDF-less
run wrote, and reading it back would pin the paper to abstract-only coding forever.

## PDF parse scoring

```
score = refs × 300 + abstract_len + intro_len × 2 + min(raw_text_len ÷ 5, 1000)
```

`parse_all()` runs six methods and `best_parse_result()` picks the winner by that
formula — the same one behind the ★ USED BY LLM badge in the app. The winner's text
goes to the DOI-resolution LLM, and its references become the structured reference
list in the prompt.

## Caching

Every LLM call is keyed with `content_key()`, which names everything the answer depends
on: the prompt's version (`prompt_version()`, the hash of the prompt text and every
fragment it splices in), the model(s) that will answer, and the inputs actually sent.
Editing a prompt therefore invalidates exactly the caches that depended on the old
wording, with nothing to bump by hand.

| Cache | Contents |
| ----- | -------- |
| `cache/llm/classify_*.json` | front-door verdicts (complete screens only) |
| `cache/llm/reftarget_*.json` | reference-list target picks |
| `cache/llm/llm_*.json` | abstract-level and full-text identification |
| `cache/llm/outcome_*.json` | outcome verdicts, including escalations |
| `cache/llm/match_type_*.json` | match-type classifications |
| `cache/parse/parse_*.json` | per-method parse results |

Declines are cached; API failures are not.

## Post-run quarantine (`extract/sanity_check.py`)

Runs at the end of every `run_extract` and standalone. Rows that do not belong in the
resolved set are moved out to set-aside CSVs, **first match wins**, in this order:

| Bucket | Destination | Rule |
| ------ | ----------- | ---- |
| `screen_disagreement` | `screen_disagreement.csv` | `link_method == screen_disagreement` |
| `not_a_replication` | `not_a_replication.csv` | `outcome == not_a_replication` |
| `non_article` | `not_a_replication.csv` | `doi_r` is a figshare data record / peer-review object |
| `self_link` | `unresolved_self_links.csv` | `doi_o == doi_r` |
| `doi_mismatch` | `unresolved_doi_mismatch.csv` | `doi_o_verification == mismatch` |
| `title_search_provisional` | `provisional_title_search.csv` | `link_method == llm_title_search` |
| `target_pending` | `target_pending.csv` | `link_method == target_pending` |
| `fabricated_doi_o` | `fabricated_original_doi.csv` | `--deep` only: `doi_o` present but doi.org 404s |

Order is load-bearing. Disagreements are claimed first so that a disagreement row whose
outcome happened to be coded `not_a_replication` cannot land in the agreed-no file and
bias any precision computed over it.

`cannot_be_determined` rows stay in `extracted.csv` — a linked original with an
undecidable outcome is still a real record. Chronology errors, duplicate `pair_id`s and
blank `doi_r` are reported but never moved; the right fix depends on diagnosis.

## Test sandbox

With `--extracted-test`, output goes to `data/extracted-test.csv` and DOIs already
resolved in `extracted.csv` are skipped, so a test run does not re-process production
rows. `--doi-r` targets are always processed, sandbox or not.

```bash
python -m extract.promote_test --all           # promote all
python -m extract.promote_test --doi 10.xxx/y  # promote one DOI
python -m extract.promote_test --all --dry-run # preview
```

## Key functions

| Function | File | Description |
|----------|------|-------------|
| `run_extract()` | `extract/run_extract.py` | Main orchestrator |
| `_front_door_row()` | `extract/run_extract.py` | Turns a screen verdict into a finished row, or `None` to continue |
| `classify_match_type()` | `extract/run_extract.py` | Routing step (rules, then LLM) |
| `_resolve_and_code()` | `extract/run_extract.py` | Ladder → merge → guard → outcome for one row |
| `_outcome_without_coding()` | `extract/run_extract.py` | The outcome gate |
| `_guard_original_link()` | `extract/run_extract.py` | Self-link rejection and `doi_o` recovery |
| `classify_replication()` | `shared/llm_client.py` | Two-model front-door vote |
| `screen_references_with_llm()` | `shared/llm_client.py` | Reference-list target pick |
| `run_for_doi()` | `extract/link_original.py` | The resolution ladder |
| `identify_original_with_llm()` | `shared/llm_client.py` | Abstract-level and full-text identification |
| `run_multi_original_for_doi()` | `extract/multi_original.py` | Multi-original pipeline |
| `extract_outcome()` | `extract/code_outcome.py` | Outcome coding |
| `find_all_candidates()` | `shared/openalex_client.py` | Candidate search |
| `parse_all()` / `best_parse_result()` | `shared/pdf_parsing.py` | Run all PDF parsers, score and pick |
| `verify_and_correct()` | `shared/doi_verify.py` | `doi_o` verification |
| `run_sanity_check()` | `extract/sanity_check.py` | Post-run quarantine |
