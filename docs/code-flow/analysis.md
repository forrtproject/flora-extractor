# Analysis Scripts — Code Flow

Located in `analysis/`. These are post-extraction diagnostic tools, not part of the
main pipeline. All outputs are read-only — no modifications to `data/` files.

**The overlap / recall-gap analysis is gone.** `analysis/run_overlap_analysis.py`
and `analysis/data_loader.py` compared `data/all_replications.csv` against
`data/candidates.csv`, and both the script and that corpus are retired with the
admission-gated Stage 1: Stage 1's corpus is the survivor pool now. The per-release
recall monitor that replaces it — join the gold keys against a routing release, no
LLM and no sampling, and report per rule how many known-good papers it discarded —
is specified in `analysis/gold/README.md` and **not yet implemented**.

---

## Rule Analysis (`analysis/rule_analysis.py`)

Audits Stage 2's filter decisions (as recorded in `filtered.csv`) and the extraction
link methods.

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

Also provides `audit_extracted_csv()`, which returns a summary dict. Nothing outside
`analysis/` imports it — there is no Analysis tab in the dashboard, whose tabs are
Search, Filter, Extract, Extract-Test, Supabase plus one per set-aside file.

---

## APA Resolver (`analysis/apa_resolver.py`)

Resolves APA-format citations to structured DOIs via CrossRef.

```text
apa_resolver.py
    │
    ├── load_missing_dois()          # no arguments; reads the configured extracted.csv
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

## Other modules in `analysis/`

Each is a one-off with its own `--help`, not part of a documented flow:
`arm_evidence.py`, `build_validated_skip.py` (materialises `data/validated_skip.csv`),
`citation_gate_analysis.py`, `purge_offpile.py`, `repair_single_vote.py`,
`rule_report.py`. There is no shared `data_loader`
module any more — each script reads the CSV it needs directly.

Three sub-packages hold the evidence other docs cite, each with its own README or
report: `analysis/prescreen_eval/` (the cheap-tier evaluation behind
`limitations.md` §g), `analysis/screening_eval/` (the front-door prompt and gate
sweeps — `prompt_v33.txt`, `report_v33.md`, `report_v32.md`, `gate_sweep_v32.md`),
and `analysis/osf_registrations/` (the OSF registration census). `analysis/gold/`
holds the specification for the per-release recall monitor, which is not yet built.

---

## Outputs

| File | Description |
| --- | --- |
| `analysis/extraction_audit.md` | Link method and confidence breakdown for extracted.csv |
| `analysis/rule_improvement_opportunities.csv` | Ranked filter/extract improvement suggestions |
| `analysis/apa_reference_fallback.csv` | Manual APA reference entries |
| `analysis/rescan_*.csv` | Impact reports from the 2026-08 rescan |

Most of these are committed, not gitignored: `extraction_audit.md`, the two
`rescan_*.csv` impact reports and eleven other CSVs under `analysis/` are tracked,
because they are the evidence other documents cite. `analysis/apa_reference_fallback.csv`
is the one that is ignored (`.gitignore`). The `gap_summary.md` / `gap_analysis_*.csv`
files belonged to the retired overlap analysis and are no longer produced.
