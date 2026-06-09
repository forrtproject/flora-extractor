# Analysis Scripts — Code Flow

Located in `analysis/`. These are post-extraction diagnostic tools, not part of the main pipeline.

---

## Gap Analysis (`analysis/gap_analysis.py`)

Compares `data/extracted.csv` against the FLoRA entry sheet to find studies that are in FLoRA but not yet extracted.

```
gap_analysis.py
    │
    ├── load extracted.csv (doi_r column)
    ├── load data/flora_entry_sheet.csv (doi_r column)
    │
    ├── match by DOI (exact, after clean_doi normalization)
    │       → gap_analysis_doi_matched.csv
    │
    ├── match by URL (fallback when DOIs differ by formatting)
    │       → gap_analysis_url_matched.csv
    │
    └── write gap_summary.md (human-readable summary)
```

**Outputs:** `analysis/gap_analysis_doi_matched.csv`, `analysis/gap_analysis_url_matched.csv`, `analysis/gap_summary.md`

---

## Rule Analysis (`analysis/rule_analysis.py`)

Audits the filter rules in `filter/rule_filter.py`.

```
rule_analysis.py
    │
    ├── load filtered.csv + extracted.csv
    │
    ├── link_method_distribution()
    │       → count rows per link_method in extracted.csv
    │
    ├── find_missing_doi_rows()
    │       → rows where doi_o is empty
    │
    └── analyze_confidence_distribution()
        → breakdown of link_confidence values
```

Also provides `audit_extracted_csv()` which returns a summary dict suitable for dashboards or logging.

---

## APA Resolver (`analysis/apa_resolver.py`)

Resolves APA-format citations (e.g. "Smith, J. (2018). Title. Journal, 10(2), 1-10.") to structured DOIs via CrossRef.

```
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
    └── write apa_reference_fallback.csv
            doi_r, resolved_apa_doi, apa_reference, crossref_score
```

Used to fill in missing `doi_o` values when the main pipeline couldn't find the original.

---

## Data Loader (`analysis/data_loader.py`)

Shared CSV loader used by all analysis scripts:

```python
from analysis.data_loader import load_candidates, load_filtered, load_all_replications

df_cands    = load_candidates()   # data/candidates.csv
df_filtered = load_filtered()     # data/filtered.csv
df_allrep   = load_all_replications()  # data/all_replications.csv
```

All loaders normalise DOIs via `clean_doi()` and return DataFrames.

---

## Outputs (gitignored)

All analysis output files are gitignored by default (see `.gitignore`):

```
analysis/gap_analysis_*.csv
analysis/gap_vs_extracted_comparison.csv
analysis/filter_rules.csv
analysis/filter_misclassifications.csv
analysis/rule_improvement_opportunities.csv
analysis/source_contribution.csv
analysis/apa_reference_fallback.csv
```

The `.md` summary files (`gap_summary.md`, `extraction_audit.md`) are committed.
