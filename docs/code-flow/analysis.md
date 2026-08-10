# Analysis Scripts — Code Flow

Located in `analysis/`. These are read-only diagnostic tools, not part of the main
pipeline. No module under `analysis/` is imported by `search/`, `filter/`, `extract/`
or `validate/`, and nothing here modifies `data/` files.

**The inventory lives in [`analysis/README.md`](../../analysis/README.md)** — what each
script and sub-directory holds, and what was removed and why. This page documents the
one script with a flow worth drawing.

**The overlap / recall-gap analysis is gone.** `analysis/run_overlap_analysis.py`
and `analysis/data_loader.py` compared `data/all_replications.csv` against
`data/candidates.csv`, and both the script and that corpus are retired with the
admission-gated Stage 1: Stage 1's corpus is the survivor pool now. The per-release
recall monitor that replaces it — join the gold keys against a routing release, no
LLM and no sampling, and report per rule how many known-good papers it discarded —
is specified in `analysis/gold/README.md` and **not yet implemented**.

---

## APA Resolver (`analysis/apa_resolver.py`)

Resolves replications that have no `doi_o` to a DOI via CrossRef, to fill in originals
the main pipeline could not find.

```text
apa_resolver.py
    │
    ├── load_missing_dois()          # no arguments; reads the configured extracted.csv
    │       → rows where doi_o is empty
    │
    ├── format_apa_reference(metadata)
    │       → "Authors (Year). Title. Journal."
    │
    ├── for each row:
    │       query_crossref()   → /works?query.bibliographic=<apa_ref>
    │       fuzzy_match_csv()  → the manual analysis/apa_reference_fallback.csv tier
    │
    └── run_apa_resolution()
            → analysis/missing_dois_resolved.csv
            → analysis/apa_resolver_report.md
```

It is a **library, not a command**: there is no `__main__` block, so `python -m
analysis.apa_resolver` does nothing. Import `resolve_all()` or `run_apa_resolution()`.

---

## Other modules in `analysis/`

Each is a one-off with its own `--help`, not part of a documented flow:
`arm_evidence.py` and `rule_report.py` (the two live rule-evidence tools, both written
up in `analysis/README.md`), `build_validated_skip.py` (materialises
`data/validated_skip.csv`), `citation_gate_analysis.py`, `repair_single_vote.py`.
There is no shared `data_loader` module any more — each script reads the CSV it needs
directly.

Four sub-packages hold the evidence other docs cite, each with its own README or
report: `analysis/prescreen_eval/` (the cheap-tier evaluation behind
`limitations.md` §g), `analysis/screening_eval/` (the front-door prompt and gate
sweeps — `prompt_v33.txt`, `report_v33.md`, `gate_sweep_v32.md`),
`analysis/stage3_eval/` (the Stage 3 resolution-quality iteration log, `REPORT.md`),
and `analysis/osf_registrations/` (the OSF registration census). `analysis/gold/`
holds the specification for the per-release recall monitor, which is not yet built.

The superseded run records behind those reports — the per-prompt, per-model JSON files
and the rendered Stage 3 payloads — now live under `archive/`, paths mirrored; see
[`archive/README.md`](../../archive/README.md).

---

## Outputs

The tracked files under `analysis/` are the sub-packages' reports, case sets and coding
CSVs, plus `analysis/osf_registrations/*.csv` — they are committed because other
documents cite them. Ignored: `analysis/apa_reference_fallback.csv` (root
`.gitignore`), `analysis/citation_gate_out/`, `analysis/prescreen_eval/override_*.json`
and `analysis/stage3_eval/.spend_*.json`. `apa_resolver`'s own outputs
(`missing_dois_resolved.csv`, `apa_resolver_report.md`) are untracked; the
`gap_summary.md` / `gap_analysis_*.csv` files belonged to the retired overlap analysis
and are no longer produced.

There is no Analysis tab in the dashboard, whose tabs are Search, Filter, Extract,
Extract-Test, Supabase plus one per set-aside file.
