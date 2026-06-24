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
| `source` | string | Where this paper was discovered: `openalex`, `semantic_scholar`, `engine`, `bob_reed`, `i4r` |
| `ref_r` | string | Formatted reference string for the replication paper |

---

## filtered.csv (Stage 2 → Stage 3)

All `candidates.csv` columns, plus:

| Column | Type | Description |
| ------ | ---- | ----------- |
| `filter_status` | string | `replication` \| `reproduction` \| `false_positive` \| `needs_review` |
| `filter_method` | string | `rule_based` \| `llm` \| `both` |
| `filter_evidence` | string | Phrase or pattern that triggered the classification |
| `filter_confidence` | string | `high` \| `medium` \| `low` — categorical, not a float |

`filter_confidence` is a three-level label because a single LLM call cannot produce calibrated probabilities.

---

## extracted.csv (Stage 3 → web app)

All `filtered.csv` columns, plus:

| Column | Type | Description |
| ------ | ---- | ----------- |
| `pair_id` | string | Hash of `(doi_r, doi_o)` — unique row key; recomputed if `doi_o` is corrected |
| `original_match_type` | string | `single_original` \| `multiple_match` \| `multiple_original` |
| `original_match_confidence` | string | `high` \| `medium` \| `low` |
| `doi_o` | string | DOI of the original (target) study |
| `title_o` | string | Title of the original study |
| `year_o` | int | Publication year of the original study |
| `authors_o` | string | Authors of the original study (semicolon-separated surnames) |
| `ref_o` | string | Formatted reference string for the original study |
| `link_method` | string | How the original was found — see below |
| `link_evidence` | string | Quote or description supporting the link |
| `link_confidence` | string | `high` \| `medium` \| `low`; downgraded to `low` on DOI mismatch |
| `link_llm_model` | string | Model name used for LLM linking; blank for rule-based rows |
| `doi_o_verification` | string | DOI verification status — see below |
| `outcome` | string | Replication outcome — see below |
| `outcome_phrase` | string | Verbatim phrase from paper describing outcome |
| `outcome_confidence` | string | `high` \| `medium` \| `low` |
| `out_quote_source` | string | Where the outcome quote came from: `abstract` \| `fulltext` |
| `outcome_reasoning` | string | LLM chain-of-thought for the outcome decision |
| `type` | string | `replication` \| `reproduction` |
| `original_rank` | int | 1 for single-original; 1, 2, 3… for multi-original |
| `n_originals` | int | Total number of originals for this paper |

### `link_method` values

| Value | Meaning |
| ----- | ------- |
| `author_year_match` | Matched via author names + year (rule-based, no LLM) |
| `llm_abstract` | LLM resolved the original from abstract text |
| `llm_fulltext` | LLM resolved the original from full PDF text |
| `no_original_found` | Pipeline could not identify an original study |
| `target_pending` | Original DOI must be supplied manually |
| `api_error` | Extraction failed after retries |

### `doi_o_verification` values

Populated automatically before each row is written. See [doi-verification.md](doi-verification.md) for full design.

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

| Value | Meaning |
| ----- | ------- |
| `success` | Replication confirmed the original finding |
| `failure` | Replication failed to find the original effect |
| `mixed` | Some aspects replicated, others did not |
| `uninformative` | Authors explicitly state outcome is unclear |
| `cannot_be_determined` | Insufficient detail in abstract to classify |
| `descriptive` | Adapted methods in a new context, does not test original claim |
| `pending` | Outcome not yet extracted |
| `api_error` | Extraction failed after retries |

---

## validated.csv (Stage 4 output)

All `extracted.csv` columns, plus:

| Column | Type | Description |
| ------ | ---- | ----------- |
| `validation_status` | string | `confirmed` \| `rejected` \| `pending` \| `needs_review` |
| `vote_count` | int | Number of votes cast |
| `confirm_votes` | int | Votes confirming the extraction |
| `reject_votes` | int | Votes rejecting the extraction |
| `validator_notes` | string | Free-text notes from reviewers |
| `validated_doi_o` | string | Reviewer-corrected original DOI (blank = accepted unchanged) |
| `validated_outcome` | string | Reviewer-corrected outcome (blank = accepted unchanged) |

`validated_doi_o` and `validated_outcome` enable accuracy measurement by diffing against the extracted values.
