# `analysis/` — read-only diagnostics and evaluation evidence

Nothing here is part of the pipeline. Every script reads `data/` and writes only
into `analysis/`; no module under `analysis/` is imported by `search/`, `filter/`,
`extract/` or `validate/`.

Two sub-directories hold the **evidence behind shipped decisions** and are the
things to read first; the rest are one-off diagnostics kept for their findings.

## Evaluation evidence (live)

| Path | What it holds |
| --- | --- |
| `prescreen_eval/` | The issue #130 evaluation of the optional cheap pre-screen (`shared/prescreen.py`). `REPORT.md` is the finding and is cited from CLAUDE.md; `CASESETS.md` and `README.md` describe the gold sets; `build_casesets.py` / `enrich_casesets.py` / `eval_prescreen.py` rebuild them; the `pre_p*_*.json` files are per-prompt, per-model run records. |
| `screening_eval/` | The derivation of Stage 3's front-door voter pair and its prompt. `report_v33.md` scores the shipped v3.3 prompt (`prompt_v33.txt` is the evaluated copy of `_CLASSIFY_PROMPT`); `report_v32.md` is the version behind it. `gate_sweep*.md` derive `screen_gate()`; `human_truth*.json` / `heldout_truth*.json` are the hand-coded labels; `voter_*.json` are per-prompt, per-model run records. |

## One-off analyses (historical)

| File | What it did |
| --- | --- |
| `apa_resolver.py` | Resolves replications that have no DOI via CrossRef (title + authors + year), with a manual `apa_reference_fallback.csv` tier, and formats APA references. Reads `all_replications.csv`. |
| `rule_analysis.py` | Audits `extracted.csv` by link method, confidence and missing `doi_o` → `extraction_audit.md`. |
| `rescan_impact_report.py` | Impact of the 2026-07-08 narrative-citation fix on Stage 3 re-linking. Its filter-gate half was removed with the rule filter it audited (#146) — a rule change is now measured with `python -m filter.engine diagnose`. |
| `citation_gate_analysis.py` | A pre-#152 measurement of the retired Stage 2 rule filter's author-year cite gate. Kept for its numbers; the code path it measured no longer exists. |

## What was removed, and why

Stage 1 is now a snapshot scan into the survivor pool, and `data/candidates.csv`
no longer exists. Everything in `analysis/` that was built on it went with it —
recoverable from git history, listed here so its absence is not mistaken for a
gap:

- **The overlap-analysis cluster** (`run_overlap_analysis.py`, `analyses.py`,
  `data_loader.py`, `matching.py`, `output_writer.py`, and the `gap_summary.md` /
  `gap_analysis_*.csv` outputs). It measured which known replications were absent
  from `candidates.csv`, matching by DOI, then URL, then fuzzy title+year+author.
- **`old_pipeline_compare.py`** and `old_pipeline_comparison.json`. It compared the
  old R pipeline against `candidates.csv` and imported the search phrase list from
  `search/openalex_search.py`; both sides of the comparison are gone.
- **`phrase_coverage_analysis.py`** and `phrase_coverage_recovery.csv`. It measured
  the coverage of a phrase list that no longer exists — the search gate is a stem
  alternation over the whole corpus, not a list of phrases.
- **Four 4-byte stub CSVs** (`filter_rules.csv`, `filter_misclassifications.csv`,
  `gap_analysis_fuzzy_title.csv`, `source_contribution.csv`) — a bare BOM each, a
  placeholder rather than a result.

## Running one

```bash
python -m analysis.rule_analysis          # → extraction_audit.md
python -m analysis.rescan_impact_report
```

Inputs come from `shared.config.DATA_DIR`, logging from `shared.config.log`.
Requires `pandas` and `requests` (plus `fuzzywuzzy` for `apa_resolver`'s optional
fuzzy tier). `analysis/citation_gate_out/` is gitignored.
