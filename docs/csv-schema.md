# CSV Schema

The authoritative schema definition is `shared/schema.py`. This document is the human-readable reference. If there is any discrepancy, `schema.py` wins.

---

## candidates.csv (Stage 1 → Stage 2)

Produced by `search/run_search.py`. One row per discovered paper.

| Column | Type | Description |
| ------ | ---- | ----------- |
| `doi_r` | string | DOI of the replication paper (cleaned via `clean_doi()`) |
| `title_r` | string | Paper title |
| `abstract_r` | string | Abstract text |
| `year_r` | int | Publication year |
| `authors_r` | string | Author list (semicolon-separated surnames) |
| `journal_r` | string | Journal or venue name |
| `url_r` | string | Canonical URL |
| `openalex_id_r` | string | OpenAlex work ID (e.g. W1234567890) |
| `source` | string | Where this paper was discovered: `openalex`, `openalex_concept`, `semantic_scholar`, `backfill_old_pipeline` (see `schema.SOURCE_VALUES`). `bob_reed` / `i4r` are **not** produced — their scrapers exist but are not wired into `run_search` |
| `ref_r` | string | Formatted reference string for the replication paper |

---

## filtered.csv (Stage 2 → Stage 3)

All `candidates.csv` columns, plus:

| Column | Type | Description |
| ------ | ---- | ----------- |
| `filter_status` | string | `replication` \| `reproduction` \| `false_positive` \| `needs_review` |
| `filter_method` | string | `rule_based` \| `both` — see below |
| `filter_evidence` | string | Phrase or pattern that triggered the classification |
| `filter_confidence` | string | `high` \| `medium` \| `low` — categorical, not a float |

`filter_confidence` is a three-level label because a single LLM call cannot produce calibrated probabilities.

`run_filter` writes only two values. Every row is classified by the rule filter first
(`rule_based`), and the LLM is asked only about the rows the rules left at
`needs_review` — so an LLM verdict always lands on a row that already carries a
rule-based one, and is recorded as `both`. The third value, `llm`, is written only by
`filter/refilter_fp.py` when it re-decides a row whose verdict was already `both`.
`schema.py` lists all three.

---

## extracted.csv (Stage 3 → web app)

All `filtered.csv` columns, plus:

| Column | Type | Description |
| ------ | ---- | ----------- |
| `pair_id` | string | Hash of `(doi_r, doi_o)` — unique row key; recomputed if `doi_o` is corrected |
| `original_match_type` | string | `single_original` \| `multiple_match` \| `multiple_original` |
| `original_match_confidence` | string | `high` \| `medium` \| `low` |
| `classify_llm_model` | string | Model that classified `original_match_type`; blank when a rule fired, when `--no-llm` was set, or when the classifier LLM failed |
| `oa_work_id_r` | string | OpenAlex work ID of the replication paper, **bare** form (`W2884670852`). Derived from `openalex_id_r`, which stores the URL form; falls back to a DOI lookup |
| `oa_work_id_o` | string | OpenAlex work ID of the original study, bare form. Resolved from `doi_o` *after* DOI verification, so it always describes the DOI actually written. Blank when `doi_o` is blank or unindexed in OpenAlex |
| `doi_o` | string | DOI of the original (target) study |
| `title_o` | string | Title of the original study |
| `study_o` | string | FLoRA's `study_o`: which study **inside** the original paper is targeted, as a number — several numbers (`1, 2`) when one replication targets several studies from the same paper. Blank when the original reports a single study or the replication does not say. Only the multi-original path fills it. Not the same field as the validation DB's `study_o`, which holds a title |
| `year_o` | int | Publication year of the original study |
| `authors_o` | string | Authors of the original study (semicolon-separated surnames) |
| `ref_o` | string | Formatted reference string for the original study |
| `bibtex_ref_o` | string | BibTeX entry for the original study (`@article` when a journal is known, else `@misc`; cite key `Surname_Year`). Blank when `doi_o` was dropped as a mismatch |
| `bibtex_ref_r` | string | BibTeX entry for the replication paper, built from the Stage 1 columns. Volume/issue/pages are not tracked at Stage 1, so they are absent |
| `link_method` | string | How the original was found — see below |
| `link_evidence` | string | Quote or description supporting the link |
| `link_confidence` | string | `high` \| `medium` \| `low`; downgraded to `low` on DOI mismatch |
| `link_llm_model` | string | Model that decided the link; blank for rule-based rows. On `llm_references` rows this is the model that picked the reference, not the two classifiers that screened the paper. On `not_a_replication` and `screen_disagreement` rows it is the pair of front-door classifiers, joined with `+` (`GEMINI_LIGHT_MODEL+SCREEN_VOTER2_MODEL`) |
| `doi_o_verification` | string | DOI verification status — see below |
| `outcome` | string | Replication outcome — see below |
| `outcome_phrase` | string | Verbatim phrase from paper describing outcome |
| `outcome_confidence` | string | `high` \| `medium` \| `low` |
| `out_quote_source` | string | Where the outcome quote came from: `abstract` \| `title` \| `fulltext`. `fulltext` appears only on results escalated to the fulltext LLM pass. |
| `outcome_reasoning` | string | LLM chain-of-thought for the outcome decision |
| `type` | string | `replication` \| `reproduction` |
| `original_rank` | int | 1 for single-original; 1, 2, 3… for multi-original |
| `n_originals` | int | Total number of originals for this paper |

### `link_method` values

Each rule-based method is recorded under its own name: their reliability differs
sharply, so a consumer has to be able to tell them apart.

| Value | Meaning |
| ----- | ------- |
| `citation_context_match` | Rule-based: a parenthetical `(Author, Year, Journal)` citation in the abstract scored a single candidate above threshold with a clear gap over the runner-up |
| `same_author_year_title_overlap` | Rule-based: all candidates share one author + year; chosen by title-Jaccard overlap with abstract/title |
| `single_candidate_after_requery` | Rule-based: exactly one OpenAlex candidate remained after re-query, auto-accepted at score 1.0 with **no semantic check** (weakest of the rule-based methods) |
| `title_pattern_match` | Rule-based: the replication title (e.g. "A Replication of X") named the original, matched to a candidate by title Jaccard |
| `grobid_ref_match` | Rule-based: a GROBID-parsed reference matched a candidate by DOI or author+year. The resolver behind it (`shared/disambiguation.resolve_by_grobid_refs`) is not wired into `run_for_doi`, so only stored rows carry this value |
| `llm_cited_candidates` | LLM chose the original from candidates found by matching an author-year citation in the abstract against the paper's references |
| `llm_references` | LLM picked the original from the paper's full OpenAlex reference list, accepted only at high confidence (Stage 4.5 screen) |
| `not_a_replication` | Stage 3's front-door screen concluded at high confidence that the paper does not replicate or reproduce anything; no original exists to link and no PDF was fetched |
| `llm_fulltext` | LLM resolved the original from full PDF text (also multi-original rows when a PDF/GROBID fed the prompt) |
| `llm_title_search` | **Provisional, not resolved.** The LLM named an original that was **not** in the candidate/reference list, so the DOI came from a CrossRef/OpenAlex title search against the whole literature. Two points in the ladder search this way and both record this one value: the pre-PDF search on a target the screen named but could not match to a reference (gated on both voters calling the paper a replication at high confidence), and the search after the full-text LLM names a title that is in no candidate list. A hand-check of the 2026-07-28 batch put precision near 50%, and the errors are invisible to `doi_o_verification` (the DOI does resolve to the named title; the named title is simply not the paper's target). `link_confidence` is forced to `low`, no outcome is coded, and `sanity_check` quarantines the row to `data/provisional_title_search.csv` for human confirmation |
| `screen_disagreement` | The two front-door "is this a replication?" classifiers disagreed; the row is set aside for review rather than processed further, and `sanity_check` quarantines it to `data/screen_disagreement.csv` |
| `author_year_match_legacy` | Legacy row written before the split; the specific rule-based method cannot be recovered retroactively (see `tools/migrate_link_methods.py`) |
| `no_original_found` | Pipeline could not identify an original study |
| `target_pending` | Original DOI must be supplied manually. Also written when only one of the two front-door classifiers answered — a single vote carries no agreement signal, so the row waits for a re-run instead of being filed as a disagreement |
| `api_error` | Extraction failed after retries, including a front-door screen where **both** classifiers failed |

The enum lives in `shared/schema.py` as `LINK_METHOD_VALUES`. The subset that counts
as a resolved link — the rows `extract/csv_to_db.py` imports into the validation DB —
is `RESOLVED_LINK_METHODS`: the five rule-based methods plus `llm_cited_candidates`,
`llm_references` and `llm_fulltext`. Every other value above marks
a row that is unresolved, quarantined, or a pipeline-state marker, and is never
imported. `tests/test_extract.py::test_map_method_outputs_are_in_link_method_enum`
asserts that every value `_map_method` can emit is in `LINK_METHOD_VALUES`, so a new
resolution method cannot silently fall outside the enum.

### `doi_o_verification` values

Populated automatically before each row is written. The matching thresholds and the three
correction tiers live in `shared/doi_verify.py`; *DOI Verification* in `CLAUDE.md`
summarises the design.

| Value | Meaning |
| ----- | ------- |
| `verified` | CrossRef/OpenAlex metadata matches expected title/year |
| `corrected` | DOI was wrong or blank; a confident replacement was found and substituted |
| `mismatch` | Metadata disagrees with expected; no confident replacement; `link_confidence` → `low` |
| `no_doi` | Original found in OpenAlex but has no registered DOI |
| `not_found` | DOI was blank and no match could be found anywhere |
| `no_metadata` | DOI is registered but returned no usable metadata |
| `api_error` | CrossRef and OpenAlex both failed after retries |
| `skipped` | Row is `target_pending` or `api_error`; nothing to verify |

### `outcome` values

The `type` column selects the vocabulary. A **replication** is coded on the categories
below (`schema.OUTCOME_CATEGORIES`); a **reproduction** re-runs the original data and
code, so it is coded on the 3×3 computation/robustness grid instead
(`schema.REPRODUCTION_OUTCOME_CATEGORIES`, matching the FLoRA entry form's dropdown):
`computationally successful` \| `computational issues` \| `computation not checked`,
each combined with `robust` \| `robustness challenges` \| `robustness not checked` —
e.g. `computationally successful, robustness not checked`. `cannot_be_determined` and
`not_a_replication` are valid for both. `schema.outcome_categories_for(type)` returns
the applicable set.

`pending` and `api_error` are **pipeline-state markers**, not outcomes — they record
where a row sits in the pipeline.

| Value | Meaning |
| ----- | ------- |
| `success` | Replication confirmed the original finding |
| `failure` | Replication failed to find the original effect |
| `mixed` | Some aspects replicated, others did not |
| `descriptive` | Adapted methods in a new context, does not test original claim |
| `statistically_successful_but_flawed` | The original effect was obtained, but the paper's main message is that the method does not validly test the claim |
| `uninformative` | The **authors themselves** report that their attempt cannot speak to the original — underpowered, design failure, evidence neither confirming nor contradicting |
| `cannot_be_determined` | **We** could not reach a verdict from the text available to the pipeline. Not the same as `uninformative`: that is the paper's conclusion, this is our extraction falling short |
| `not_a_replication` | Text does not describe a genuine attempt to replicate/reproduce the named original (unrelated, biological/technical, or metaphorical use of "replicate"/"reproduce") |
| `pending` | Outcome not coded (pipeline-state marker). Written for every row whose `link_method` is not in `RESOLVED_LINK_METHODS` — there is no confirmed original to code an outcome against, so the outcome LLM never runs (`outcome_reasoning` says which method it was) |
| `api_error` | Extraction failed after retries (pipeline-state marker) |

> `uninformative` and `statistically_successful_but_flawed` are FLoRA codebook
> categories that the pipeline could not emit until the rule-alignment pass.
> `uninformative` had been retired into `OUTCOME_LEGACY_VALUES` and folded into
> `cannot_be_determined`, which merged a property of the paper with a limit of our
> extraction; it is a live category again, and `OUTCOME_LEGACY_VALUES` is now empty.

---

## Stage 4 output — Supabase, not a CSV

There is **no `data/validated.csv`**. The old SQLite/CSV voting output has been removed.
Human validation runs in a **separate repo backed by Supabase**.

`extract/csv_to_db.py` pushes resolved `extracted.csv` rows (those with
`filter_status ∈ {replication, reproduction}` and a resolved `link_method`) into three
Supabase tables:

| Table | Rows per record | Contents |
| ----- | --------------- | -------- |
| `unvalidated` | 1 | The record shown to validators (`doi_r`/`study_r`/`doi_o`/`study_o`/`outcome`/…), `validation_status = 'unvalidated'` |
| `record_metadata` | 1 | Supplementary extraction fields (filter/link/outcome method, confidence, model, ranks) |
| `validation_queue` | 3 | One slot each for `human_1`, `human_2`, `llm` |

The **final artifact is the Supabase `validated` table**, populated by the separate
validation app. The monitoring dashboard in this repo reads Supabase KPIs from those
tables via `shared/supabase_client.py`. See [`supabase-schema.md`](supabase-schema.md)
for the full table definitions.
