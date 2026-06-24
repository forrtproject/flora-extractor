# Analysis Scripts — Code Flow

Located in `analysis/`. These are post-extraction diagnostic tools, not part of the main pipeline.
All outputs are read-only — no modifications to `data/` files.

---

## Overlap / Recall Gap Analysis (`analysis/run_overlap_analysis.py`)

Compares `data/all_replications.csv` (ground-truth reference set of known replications)
against `data/candidates.csv` (Stage 1 output) to measure recall and find genuine gaps.

```bash
python -m analysis.run_overlap_analysis
```

```text
run_overlap_analysis.py
    │
    ├── analyze_recall_gap()
    │       _build_candidate_index_sets()
    │           read candidates.csv in 50k-row chunks
    │           build doi_set  (cleaned doi_r values)
    │           build url_set  (url_r + openalex_id_r — both formats indexed)
    │
    │       load all_replications.csv
    │
    │       for each row in all_replications:
    │           if doi_r in doi_set → matched, skip (not a gap)
    │           if url_r in url_set → matched, skip (not a gap)
    │           else → genuine gap:
    │               has doi_r  → gaps_by_doi
    │               has url_r  → gaps_by_url
    │               neither   → gaps_by_doi
    │
    ├── analyze_filter_gap()        (placeholder — always returns empty)
    ├── analyze_source_contribution() (placeholder)
    └── analyze_filter_rules()      (placeholder)
    │
    └── write_gap_csv() + write_report_markdown()
            → analysis/gap_analysis_doi_matched.csv
            → analysis/gap_analysis_url_matched.csv
            → analysis/gap_summary.md
```

### How to read the gap counts

The gap count in `gap_summary.md` means papers in `all_replications.csv` that are
**genuinely absent** from `candidates.csv`. Both DOI and OpenAlex work ID (`openalex_id_r`)
are checked so papers found under either identifier are not double-counted as gaps.

`all_replications.csv` contains a mix of `validation_status` values — only a subset
are confirmed true replications:

| `validation_status` | Meaning for gap analysis |
| --- | --- |
| `llm_confirmed` | Confirmed replications — genuine gaps worth investigating |
| `false_positive` | Correctly absent; Stage 1 phrase search rightly excluded them |
| `needs_review` | Unknown; may or may not be real gaps |
| `already_in_flora` | Already in the database; absence from candidates is expected |

The `gap_analysis_doi_matched.csv` file includes a `validation_status` column so you
can filter to `llm_confirmed` rows when prioritising which gaps to close.

### Why gaps exist — root causes (as of 2026-06-23)

| Root cause | Gap count | Fix |
| --- | --- | --- |
| Papers with no replication keyword in title or abstract | ~266 confirmed | External list ingestion (Fix 1) |
| Abstract-only replication language not covered by phrases | ~128 confirmed | 13 new phrases added (Fix 3) |
| Papers classified by OpenAlex concept but not phrase-matched | remaining | Concept search added (Fix 2) |
| URL format mismatch (old pipeline stored `openalex.org/W…` as `url_r`) | was ~2,847 false gaps | Fixed in `_build_candidate_index_sets` (Fix 4) |

### What the URL index fix does (Fix 4)

The old pipeline stored OpenAlex work IDs (`https://openalex.org/W…`) as `url_r`.
The new pipeline stores open-access URLs or landing page URLs instead.
`_build_candidate_index_sets()` now indexes **both** `url_r` and `openalex_id_r`
from candidates.csv, so papers present under either URL form are correctly marked as
found rather than reported as gaps.

---

## Rule Analysis (`analysis/rule_analysis.py`)

Audits the filter rules in `filter/rule_filter.py` and the extraction link methods.

```text
rule_analysis.py
    │
    ├── load filtered.csv + extracted.csv
    │
    ├── analyze_link_method_distribution()
    │       → count rows per link_method in extracted.csv
    │
    ├── find_missing_doi_rows()
    │       → rows where doi_o is empty
    │
    ├── analyze_confidence_distribution()
    │       → breakdown of link_confidence values
    │
    └── generate_improvement_opportunities()
        → analysis/rule_improvement_opportunities.csv
```

Also provides `audit_extracted_csv()` which returns a summary dict used by the
dashboard's Analysis tab.

---

## APA Resolver (`analysis/apa_resolver.py`)

Resolves APA-format citations to structured DOIs via CrossRef.

```text
apa_resolver.py
    │
    ├── load_missing_dois(extracted_csv)
    │       → rows where doi_o is empty
    │
    ├── format_apa_reference(row)
    │       → "Authors (Year). Title. Journal."
    │
    ├── for each row:
    │       query CrossRef /works?query.bibliographic=<apa_ref>
    │       extract top match DOI
    │       cache result
    │
    └── write analysis/apa_reference_fallback.csv
```

Used to fill in missing `doi_o` values when the main pipeline couldn't find the original.

---

## Data Loader (`analysis/data_loader.py`)

Shared CSV loader used by all analysis scripts:

```python
from analysis.data_loader import load_candidates, load_filtered, load_all_replications

df_cands    = load_candidates()        # data/candidates.csv
df_filtered = load_filtered()          # data/filtered.csv
df_allrep   = load_all_replications()  # data/all_replications.csv
```

All loaders normalise DOIs via `clean_doi()` and return DataFrames.

> **Note:** `load_candidates()` loads the full CSV into memory. For large runs
> (candidates.csv ≥ 1M rows) use chunked reading directly or call
> `_build_candidate_index_sets()` from `analysis/analyses.py` instead.

---

## Outputs

| File | Description |
| --- | --- |
| `analysis/gap_summary.md` | Human-readable recall gap report with counts and samples |
| `analysis/gap_analysis_doi_matched.csv` | Gaps where the reference has a DOI |
| `analysis/gap_analysis_url_matched.csv` | Gaps where the reference has a URL but no DOI |
| `analysis/extraction_audit.md` | Link method and confidence breakdown for extracted.csv |
| `analysis/rule_improvement_opportunities.csv` | Ranked filter/extract improvement suggestions |
| `analysis/filter_misclassifications.csv` | Replications discovered but wrongly filtered out |
| `analysis/source_contribution.csv` | Per-source recall contribution (placeholder) |
| `analysis/apa_reference_fallback.csv` | Manual APA reference entries |

All CSV outputs and `gap_analysis_*.csv` are gitignored. The `.md` summary files
(`gap_summary.md`, `extraction_audit.md`) are committed so the dashboard Analysis
tab can read them without re-running the analysis.
