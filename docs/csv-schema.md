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
| `filter_status` | string | `replication` \| `reproduction` \| `false_positive` \| `needs_review` — the paper-type field; see below |
| `filter_method` | string | `rule_based` \| `screen` — see below |
| `filter_evidence` | string | Phrase or pattern that triggered the classification |
| `filter_confidence` | string | `high` \| `medium` \| `low` — categorical, not a float |

`filter_confidence` is a three-level label because a single LLM call cannot produce calibrated probabilities.

`run_filter` writes one value: every row is classified by the deterministic rule
filter (`rule_based`), and rows the rules cannot decide are written through as
`needs_review`. Stage 3's front-door screen is the validated decider of "is this a
replication at all", so when it passes a row, `run_extract` overwrites
`filter_status` with the screen's paper type (`replication` / `reproduction`) and
sets `filter_method` to `screen`, recording which call made the call. When the gate
proceeds without any qualifying vote (unclear/unclear, or an unconfident `none`
against an unconfident qualifying answer) no call has said what the paper is, so both
fields keep Stage 2's values and `type` is left empty. Such a row is resolved and
outcome-coded but stays at `needs_review`, which `csv_to_db` does not import: it waits
on the check page for a human to say what it is.

`llm` and `both` are **historical**: Stage 2 had an LLM escalation for
`needs_review` rows, which is retired. Rows written before the v3.2 screen still
carry those values and `schema.py` still lists them.

---

## extracted.csv (Stage 3 → web app)

All `filtered.csv` columns, plus:

| Column | Type | Description |
| ------ | ---- | ----------- |
| `pair_id` | string | MD5 of `doi_r` + the original's identifier — unique row key; recomputed if `doi_o` is corrected. The identifier is `doi_o` when it is set, else `oa:<oa_work_id_o>`, else `t:<normalised title_o>`, so DOI-less originals of the same replication stay distinct instead of all hashing to the same value. The hash for any row with a `doi_o` is unchanged from before that fallback existed |
| `original_match_type` | string | **Observed, not routed**: `multiple_original` when the per-target adapter wrote more than one row for the paper, `single_original` when it wrote one. `multiple_match` is a legacy value in stored data and is never written |
| `original_match_confidence` | string | `high` when the ladder settled on an original, `low` when the row carries none. `medium` is a legacy value in stored data |
| `classify_llm_model` | string | Legacy. Always blank: no classifier routes the row any more |
| `oa_work_id_r` | string | OpenAlex work ID of the replication paper, **bare** form (`W2884670852`). Derived from `openalex_id_r`, which stores the URL form; falls back to a DOI lookup |
| `oa_work_id_o` | string | OpenAlex work ID of the original study, bare form. Normally resolved from `doi_o` *after* DOI verification, so it always describes the DOI actually written. For a DOI-less original (`doi_o_verification = no_doi`) it is the only identifier the row has: it carries the record identity, drives `pair_id`, and becomes the `url_o` validators click. Blank when there is neither a `doi_o` nor a known OpenAlex record |
| `study_r` | string | FLoRA's `study_r`: which study **inside this replication** re-tests the original named on this row, as a number — several numbers (`1, 2`) when several do. Blank when the paper reports a single study or does not say. Filled from the target prompt's `replication_study_numbers` on both link paths; blank on rungs that resolve without an LLM. The counterpart of `study_o`, and what makes a multi-original paper readable |
| `doi_o` | string | DOI of the original (target) study |
| `title_o` | string | Title of the original study |
| `study_o` | string | FLoRA's `study_o`: which study **inside** the original paper is targeted, as a number — several numbers (`1, 2`) when one replication targets several studies from the same paper. Blank when the original reports a single study or the replication does not say. Filled from the target prompt's `study_numbers` on both link paths; blank on rungs that resolve without an LLM. A study identifier, never a title (issue #103) |
| `year_o` | int | Publication year of the original study |
| `authors_o` | string | Authors of the original study (semicolon-separated surnames) |
| `ref_o` | string | Formatted reference string for the original study |
| `bibtex_ref_o` | string | BibTeX entry for the original study (`@article` when a journal is known, else `@misc`; cite key `Surname_Year`). Blank when `doi_o` was dropped as a mismatch |
| `bibtex_ref_r` | string | BibTeX entry for the replication paper, built from the Stage 1 columns. Volume/issue/pages are not tracked at Stage 1, so they are absent |
| `link_method` | string | How the original was found — see below |
| `link_evidence` | string | Quote or description supporting the link |
| `link_confidence` | string | `high` \| `medium` \| `low`; downgraded to `low` on DOI mismatch |
| `link_llm_model` | string | Model that decided the link; blank for rule-based rows. On `llm_references` rows this is the model that picked the reference, not the two classifiers that screened the paper. On `not_a_replication` rows it is the pair of front-door classifiers, joined with `+` (`GEMINI_LIGHT_MODEL+SCREEN_VOTER2_MODEL`) |
| `screen_categories` | string | **Multi-valued.** The `\|`-joined union of both front-door voters' category labels, in the prompt's enum order: `clearly_declared`, `self_retest`, `measurement_validation`, `context_transfer`, `incidental_finding`, `initial_validation`, `tool_benchmark`, `builds_on_literature`, `terminology_only`, `about_replication`, `other`. Filter it by substring or by splitting on `\|` — never by equality, since most rows carry two or more values. Written on every screened row, discards included; blank on `--no-llm` rows and on rows written before the v3.2 screen |
| `doi_o_verification` | string | DOI verification status — see below |
| `pdf_source` | string | **Full-text provenance.** The acquisition tier that supplied the document the row was coded from: `arxiv`, `osf`, `openalex_oa`, `unpaywall_pdf`, `semanticscholar`, `core`, `europepmc`, `landing_<host>`, `serpapi`, `playwright`, or `openalex_xml` for the OpenAlex GROBID-XML tier, which needs no PDF file. **Blank** when the ladder resolved before Stage 5 or acquired nothing — a `llm_fulltext` row with a blank `pdf_source` is a contradiction and should be treated as unverified |
| `parse_method` | string | **Full-text provenance.** The parser whose result won `best_parse_result()` and therefore produced the text the LLM read: `openalex_xml`, `pdfminer`, `grobid`, `docpluck`, `opendataloader` or `markitdown`. Blank when nothing was parsed |
| `outcome` | string | Replication outcome — see below |
| `outcome_phrase` | string | Verbatim phrase from paper describing outcome |
| `outcome_confidence` | string | `high` \| `medium` \| `low` |
| `out_quote_source` | string | Where the outcome quote came from: `abstract` \| `title` \| `fulltext`. `fulltext` appears only on results escalated to the fulltext LLM pass. |
| `outcome_reasoning` | string | LLM chain-of-thought for the outcome decision |
| `outcome_llm_model` | string | Model that coded the outcome. Can differ from `link_llm_model` within one run — the outcome step fails over to another provider when the primary's quota runs out. `keyword` on `--no-llm` rule-based rows; blank when no outcome verdict was made (`pending`, `api_error`) |
| `type` | string | `replication` \| `reproduction` \| empty. Decided by the front-door screen (a `both` classification is recorded as `replication`, since such a paper collects new data), falling back to Stage 2's `filter_status`. **Empty** when neither decided — the screen proceeded without a qualifying vote on a row Stage 2 left at `needs_review`; such a row is coded on the replication vocabulary but carries no type and is not imported. Also selects the outcome vocabulary — a reproduction is coded on the computation/robustness grid |
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
| `llm_references` | LLM picked the original from the paper's full OpenAlex reference list (Stage 4.5), accepted only when the model marked the pick `match_certain` |
| `not_a_replication` | Stage 3's front-door gate discarded the paper: both voters answered `none`, or one answered `none` confidently and the other gave a qualifying-or-unclear answer it declined to stand behind. No original exists to link and no PDF was fetched |
| `llm_fulltext` | LLM resolved the original from full PDF text (also multi-original rows when a PDF/GROBID fed the prompt) |
| `llm_title_search` | **Provisional, not resolved.** The LLM named an original that was **not** in the candidate/reference list, so the DOI came from a CrossRef/OpenAlex title search against the whole literature. Two points in the ladder search this way and both record this one value: the pre-PDF search on a target the screen named but could not match to a reference (gated on both voters calling the paper a replication at high confidence), and the search after the full-text LLM names a title that is in no candidate list. A hand-check of the 2026-07-28 batch put precision near 50%, and the errors are invisible to `doi_o_verification` (the DOI does resolve to the named title; the named title is simply not the paper's target). `link_confidence` is forced to `low`, no outcome is coded, and `sanity_check` quarantines the row to `data/provisional_title_search.csv` for human confirmation |
| `screen_disagreement` | **Historical, no longer emitted.** The front door's gate (`screen_gate()` in `shared/llm_client.py`) has no disagreement terminal state: a confident split now proceeds down the ladder. Rows written before the v3.2 screen still carry the value, `sanity_check` still quarantines them to `data/screen_disagreement.csv`, and `--rescreen` still reopens them |
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
| `no_doi` | Original has no registered DOI — a book, a chapter, or a pre-DOI-era paper. `doi_o` stays blank and identity hangs off `oa_work_id_o`: `pair_id` is hashed from it and `url_o` becomes `https://openalex.org/<W…>`. Such a row **is** importable for validation when `oa_work_id_o` is set; with `oa_work_id_o` blank there is nothing identifiable to show a validator and `audit_extracted` blocks it |
| `not_found` | DOI was blank and no match could be found anywhere |
| `no_metadata` | DOI is registered but returned no usable metadata |
| `api_error` | CrossRef and OpenAlex both failed after retries |
| `skipped` | Row is `target_pending` or `api_error`; nothing to verify |

### `outcome` values

The `type` column selects the vocabulary. A **replication** is coded on the categories
below (`schema.OUTCOME_CATEGORIES`); a **reproduction** re-runs the original data and
code, so it is coded on two independent axes instead, each in its own column with its
own quote and quote source (see the reproduction axes below). Its `outcome` is DERIVED
from those two: the settled values joined with `, `, computation first — e.g.
`computationally reproducible, robustness challenges`. Either axis at
`cannot_be_determined` derives `cannot_be_determined`, and `record_type_check=neither`
derives `not_a_replication` with both axes empty. `schema.REPRODUCTION_OUTCOME_CATEGORIES`
holds the twelve settled joins and `schema.outcome_categories_for(type)` returns the
applicable set. The nine strings of the retired 3×3 grid
(`computationally successful, …`) live on in `schema.OUTCOME_LEGACY_VALUES` for rows
already on disk.

### Reproduction outcome axes

| Column | Values |
| ------ | ------ |
| `outcome_computation` | `computationally reproducible` \| `computational issues` \| `technical failure` \| `not checked` \| `cannot_be_determined` (`schema.COMPUTATION_OUTCOME_VALUES`) |
| `outcome_computational_quote` | verbatim passage proving the computation verdict |
| `out_quote_computational_source` | `title` \| `abstract` \| `fulltext`, or two joined by ` \| ` matching the quote |
| `outcome_robustness` | `robust` \| `robustness challenges` \| `not checked` \| `cannot_be_determined` (`schema.ROBUSTNESS_OUTCOME_VALUES`) |
| `outcome_robustness_quote` | verbatim passage proving the robustness verdict |
| `out_quote_robust_source` | as `out_quote_computational_source` |

`technical failure` is the reproduction defeated by the materials — no data, no code,
an unrunnable workflow — the case the 3×3 grid could only record as a numerical
disagreement that was never observed. All six columns are empty on a replication row.

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
Supabase tables.

> **The import needs a schema change in the validation repo before it will run.**
> `unvalidated` needs `title_r` and `title_o` columns: `study_r` / `study_o` are study
> identifiers and were carrying paper titles instead. No new column is needed for
> `study_r` itself — only its value changes, from a hard-coded `""` to the number the
> target prompt reports. Until those columns exist every
> insert fails with a PostgREST "column does not exist" error — nothing is written and
> nothing is corrupted. Tracked in issue #103.

| Table | Rows per record | Contents |
| ----- | --------------- | -------- |
| `unvalidated` | 1 | The record shown to validators (`doi_r`/`title_r`/`doi_o`/`title_o`/`study_o`/`outcome`/…), `validation_status = 'unvalidated'` |
| `record_metadata` | 1 | Supplementary extraction fields (filter/link/outcome method, confidence, model, ranks) |
| `validation_queue` | 3 | One slot each for `human_1`, `human_2`, `llm` |

The **final artifact is the Supabase `validated` table**, populated by the separate
validation app. The monitoring dashboard in this repo reads Supabase KPIs from those
tables via `shared/supabase_client.py`. See [`supabase-schema.md`](supabase-schema.md)
for the full table definitions.
