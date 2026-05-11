# CSV Column Reference — FLoRA Extractor Pipeline

Each stage reads the previous stage's CSV and writes a richer one. Columns are **additive** — every output includes all columns from the input plus the new ones listed below. The authoritative source is [`shared/schema.py`](../shared/schema.py); this file explains what each column means.

---

## Pipeline at a Glance

```
Stage 1  search/        →  data/candidates.csv   (10 cols)
Stage 2  filter/        →  data/filtered.csv     (14 cols = 10 + 4)
Stage 3  extract/       →  data/extracted.csv    (36 cols = 1 pair_id + 14 + 21)
Stage 4  validate/      →  data/validated.csv    (43 cols = 36 + 7)
```

---

## Stage 1 — Search

**Script:** `python -m search.run_search`  
**Input:** OpenAlex API, Semantic Scholar API, Bob Reed list, I4R list  
**Output:** `data/candidates.csv`

Stage 1 casts a wide net. It queries multiple bibliographic sources for papers that might be replications, deduplicates by DOI then by fuzzy title, and writes one row per candidate. Precision is low here by design — Stage 2 filters.

### Output columns

| Column | Type | Description |
|---|---|---|
| `doi_r` | str | DOI of the replication/reproduction paper, cleaned (no `https://doi.org/` prefix). Primary identifier throughout the pipeline. |
| `title_r` | str | Paper title. |
| `abstract_r` | str | Abstract text. Empty if the source API did not return one. |
| `year_r` | int | Publication year. |
| `authors_r` | str | Semicolon-separated author list (`Last, First; Last, First`). |
| `journal_r` | str | Journal or venue name. |
| `url_r` | str | Open-access URL if available (arXiv, OSF, Unpaywall, etc.). Empty otherwise. |
| `openalex_id_r` | str | OpenAlex work ID (e.g. `W2741809807`). Used in Stage 3 to fetch referenced works. |
| `source` | str | Where this candidate came from. Values: `openalex` · `bob_reed` · `i4r` · `semantic_scholar`. |
| `ref_r` | str | FLoRA-style display reference: `"Surname · Year · Journal"`. Built at search time from the first author's surname, publication year, and journal. |

---

## Stage 2 — Filter

**Script:** `python -m filter.run_filter`  
**Input:** `data/candidates.csv`  
**Output:** `data/filtered.csv`

Stage 2 removes false positives. Each paper is first checked by a fast rule-based classifier (keyword patterns, author-year citation check). Papers that are clearly replications or clearly false positives are labelled immediately. Uncertain papers go to a single LLM call. The goal is to pass only genuine replications and reproductions forward.

### New columns added

| Column | Type | Values | Description |
|---|---|---|---|
| `filter_status` | str | `replication` · `reproduction` · `false_positive` · `needs_review` | Classification result. `replication` = same methods, different sample. `reproduction` = same data, re-analysis. `false_positive` = not a replication at all. `needs_review` = ambiguous; human review needed. |
| `filter_method` | str | `rule_based` · `llm` · `both` | Which classifier produced the label. `both` means the rule-based and LLM classifiers agreed. |
| `filter_evidence` | str | — | The phrase or quote from the abstract that triggered the classification. Helps reviewers understand why a paper was included or excluded. |
| `filter_confidence` | str | `high` · `medium` · `low` | Categorical confidence in `filter_status`. **Not a float** — a three-level label is more honest than a pseudo-probability from a single LLM call. |

### All columns at this stage

`doi_r`, `title_r`, `abstract_r`, `year_r`, `authors_r`, `journal_r`, `url_r`, `openalex_id_r`, `source`, `ref_r`, `filter_status`, `filter_method`, `filter_evidence`, `filter_confidence`

---

## Stage 3 — Extract

**Script:** `python -m extract.run_extract`  
**Input:** `data/filtered.csv`  
**Output:** `data/extracted.csv`

Stage 3 answers two questions for each confirmed replication: which original study does it target, and what was the outcome? It first classifies how many originals the paper targets, then routes through the appropriate pipeline. False positives (`filter_status = false_positive`) are passed through with extraction columns empty — they are included in `extracted.csv` so Stage 4 can see the full picture.

### Leading identifier

| Column | Type | Description |
|---|---|---|
| `pair_id` | str | MD5 of `doi_r + "|" + doi_o` (full 32-char hex). Uniquely identifies a replication–original pair. For false positives or unresolved rows, `doi_o` is empty so `pair_id` is derived from `doi_r` alone. The UI shows only the first 3 characters as a compact visual tag. |

### New columns added

#### Original-match routing (determined first, before any extraction)

| Column | Type | Values | Description |
|---|---|---|---|
| `original_match_type` | str | `single_original` · `multiple_match` · `multiple_original` | Classification of how many distinct original studies this paper targets. `single_original` = one clear target. `multiple_match` = 2–5 OpenAlex candidates with the same author/year (disambiguation needed). `multiple_original` = paper genuinely replicates several independent originals (produces multiple rows in the output, one per original). |
| `original_match_confidence` | str | `high` · `medium` · `low` | Confidence in the `original_match_type` classification. |

#### Original study identification

| Column | Type | Description |
|---|---|---|
| `doi_o` | str | DOI of the original (target) study, cleaned. The study this replication is testing. |
| `title_o` | str | Title of the original study. |
| `year_o` | int | Publication year of the original study. |
| `authors_o` | str | Authors of the original study (first author or full list). |
| `ref_o` | str | FLoRA-style display reference for the original study: `"Surname · Year · Journal"`. Fetched from OpenAlex after `doi_o` is resolved. Falls back to `"Surname · Year"` if the journal name cannot be retrieved. |

#### Linking — how the original was found

| Column | Type | Values | Description |
|---|---|---|---|
| `link_method` | str | `author_year_match` · `llm_abstract` · `llm_fulltext` · `target_pending` · `api_error` | How the original was identified. `author_year_match` = citation pattern matched directly. `llm_abstract` = LLM identified it from the abstract alone. `llm_fulltext` = LLM needed the full PDF text. `target_pending` = not yet processed. `api_error` = failed after 3 retries. |
| `link_evidence` | str | — | The quote or citation pattern used to link the replication to its original (e.g. `"Baumeister et al. (1998)"`). |
| `link_confidence` | str | `high` · `medium` · `low` | Confidence that the identified original is correct. |
| `link_llm_model` | str | — | Exact model identifier used for DOI resolution (e.g. `gemini-2.0-flash`). Empty when linking was rule-based. |

#### Outcome

| Column | Type | Values | Description |
|---|---|---|---|
| `outcome` | str | `success` · `failure` · `mixed` · `uninformative` · `descriptive` · `pending` · `api_error` | Replication outcome. `success` = original finding replicated. `failure` = original finding not replicated. `mixed` = partially replicated. `uninformative` = study ran but could not determine if it replicated. `descriptive` = replicated methods in a different context without testing the original claim (flag for review). `pending` = not yet processed. `api_error` = extraction failed. |
| `outcome_phrase` | str | — | A verbatim quote from the paper supporting the outcome classification. |
| `outcome_confidence` | str | `high` · `medium` · `low` | Confidence in the `outcome` classification. |
| `out_quote_source` | str | `abstract` · `fulltext` · `title` | Where in the paper the `outcome_phrase` was found. |

#### Record bookkeeping

| Column | Type | Description |
|---|---|---|
| `type` | str | `replication` or `reproduction`. Carried from Stage 2's `filter_status`. |
| `original_rank` | int | `1` for single-original papers. For multi-original papers (`multiple_original`), each original gets its own row with ranks `1`, `2`, `3`, …. |
| `n_originals` | int | Total number of originals for this replication paper. `1` for single-original papers. |

### All columns at this stage

`pair_id`,  
`doi_r`, `title_r`, `abstract_r`, `year_r`, `authors_r`, `journal_r`, `url_r`, `openalex_id_r`, `source`, `ref_r`,  
`filter_status`, `filter_method`, `filter_evidence`, `filter_confidence`,  
`original_match_type`, `original_match_confidence`,  
`doi_o`, `title_o`, `year_o`, `authors_o`, `ref_o`,  
`link_method`, `link_evidence`, `link_confidence`, `link_llm_model`,  
`outcome`, `outcome_phrase`, `outcome_confidence`, `out_quote_source`,  
`type`, `original_rank`, `n_originals`

---

## Stage 4 — Validate

**Script:** `python -m validate.import_csv` then `python -m validate.app`  
**Input:** `data/extracted.csv` (loaded into SQLite via `import_csv.py`)  
**Output:** `data/validated.csv` (exported from the web app)

Stage 4 is a Flask web app where human reviewers vote to confirm or reject each extraction. Two confirm votes (from different reviewers) set `validation_status = confirmed`. Any `needs_review` vote overrides other votes. Reviewers can also correct the extracted original DOI or outcome if Stage 3 got it wrong.

### New columns added

| Column | Type | Values | Description |
|---|---|---|---|
| `validation_status` | str | `confirmed` · `rejected` · `pending` · `needs_review` | Aggregated status from reviewer votes. `pending` = no votes yet. `needs_review` = at least one reviewer flagged it. |
| `vote_count` | int | — | Total number of votes received. |
| `confirm_votes` | int | — | Number of confirm votes. |
| `reject_votes` | int | — | Number of reject votes. |
| `validator_notes` | str | — | Aggregated free-text comments from all reviewers. |
| `validated_doi_o` | str | — | Reviewer-corrected original study DOI. **Blank means the Stage 3 value was accepted unchanged.** Non-blank values allow accuracy measurement by diffing against `doi_o`. |
| `validated_outcome` | str | — | Reviewer-corrected outcome. **Blank means the Stage 3 value was accepted unchanged.** Non-blank values allow accuracy measurement by diffing against `outcome`. |

### All columns at this stage

All 36 columns from Stage 3, plus:  
`validation_status`, `vote_count`, `confirm_votes`, `reject_votes`, `validator_notes`, `validated_doi_o`, `validated_outcome`

---

## Column Naming Conventions

| Suffix / prefix | Meaning |
|---|---|
| `_r` | Relates to the **r**eplication study (the paper doing the replicating) |
| `_o` | Relates to the **o**riginal study (the paper being replicated) |
| `validated_` | Reviewer correction; blank = Stage 3 value accepted |
| `link_` | About how the original was identified/linked |
| `filter_` | Added by Stage 2's filter classifier |
| `outcome_` | About the replication result |

## Categorical Value Summary

| Column | Valid values |
|---|---|
| `filter_status` | `replication` · `reproduction` · `false_positive` · `needs_review` |
| `filter_confidence` | `high` · `medium` · `low` |
| `original_match_type` | `single_original` · `multiple_match` · `multiple_original` |
| `link_method` | `author_year_match` · `llm_abstract` · `llm_fulltext` · `target_pending` · `api_error` |
| `link_confidence` | `high` · `medium` · `low` |
| `outcome` | `success` · `failure` · `mixed` · `uninformative` · `descriptive` · `pending` · `api_error` |
| `outcome_confidence` | `high` · `medium` · `low` |
| `out_quote_source` | `abstract` · `fulltext` · `title` |
| `type` | `replication` · `reproduction` |
| `validation_status` | `confirmed` · `rejected` · `pending` · `needs_review` |
| `source` | `openalex` · `bob_reed` · `i4r` · `semantic_scholar` |
