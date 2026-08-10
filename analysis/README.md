# `analysis/` — read-only diagnostics and evaluation evidence

Nothing here is part of the pipeline. Every script reads `data/` and writes only
into `analysis/`; no module under `analysis/` is imported by `search/`, `filter/`,
`extract/` or `validate/`.

Two sub-directories hold the **evidence behind shipped decisions** and are the
things to read first; the rest are one-off diagnostics kept for their findings.

## Evaluation evidence (live)

| Path | What it holds |
| --- | --- |
| `arm_evidence.py` | The free scorer for a candidate filter pattern — see below. |
| `rule_report.py` | The applied-rules overview for the whole live bundle — see below. |
| `prescreen_eval/` | The issue #130 evaluation of the optional cheap pre-screen (`shared/prescreen.py`). `REPORT.md` is the finding and is cited from CLAUDE.md; `CASESETS.md` and `README.md` describe the gold sets; `build_casesets.py` / `enrich_casesets.py` / `eval_prescreen.py` rebuild them. The per-prompt, per-model run records (`pre_p*_*.json`) are under `archive/analysis/prescreen_eval/`. |
| `screening_eval/` | The derivation of Stage 3's front-door voter pair and its prompt. `report_v33.md` scores the shipped v3.3 prompt (`prompt_v33.txt` is the evaluated copy of `_CLASSIFY_PROMPT`); `gate_sweep_v32.{md,py}` derive `screen_gate()`; `human_truth*.json` / `heldout_truth*.json` are the hand-coded labels. Superseded generations (v2–v3.2 prompts and reports) and the raw `voter_*.json` run records are under `archive/analysis/screening_eval/`. |

## `arm_evidence.py` — should this rule or this arm go live?

Scores one candidate pattern, or every arm of a spec, against every label the repo
already owns: how much of the survivor pool it matches (and matches *alone*), how
much of that the current routing release admitted, how many known-good FLoRA
replications it reaches exclusively, how many screen-confirmed negatives it hits,
and what the already-paid-for two-voter verdicts in `cache/llm/` said about its
rows. No LLM call, no network call, no spend; a full scan of the 2,232 pool files
takes seconds, and the elapsed scan time is printed on every run.

```bash
python -m analysis.arm_evidence --spec filter/spec/replication-claim.json
```

`--pattern 'title:<regex>'` (repeatable, prefixes `title:`/`abstract:`/`text:`,
default `text`) scores ad-hoc patterns instead — the way to compare the same
phrase in the title against the same phrase anywhere. `--and '<regex>'` ANDs a
conjunct onto every arm, `--release` pins a routing release, `--json` dumps the
table, `--all-cohorts` scores every cached-verdict cohort rather than only the
largest.

**Read its label-derived columns as optimistic.** The cached verdicts,
`data/not_a_replication.csv` and `data/extracted.csv` all describe what the OLD
filter admitted, so any precision computed from them is measured on a corpus that
the pattern's ancestors selected; and the FLoRA column is partly circular, because
much of `flora.csv` was found with these very phrases. Cells resting on fewer than
15 labelled rows are marked as decoration for the same reason. The tool ranks
candidates and gates cheap decisions. It does not replace a human-labelled
precision estimate for anything that **discards**.

## `rule_report.py` — what did every rule actually do?

`arm_evidence.py` asks about one candidate pattern before it ships;
`rule_report.py` asks about the whole shipped bundle after it has run. One row
per spec in `filter/spec/`: its pile, precedence, shadow state, vocabulary and
`measured` levels; the rows it **won** in the current routing release (it was the
highest-precedence non-shadow match) against the rows it **matched** at all; for
a draft (shadow) rule the would-win counterfactual read out of `evaluations`;
known-FLoRA works reached and reached *uniquely*; known negatives hit; and — once
a tier has been screened — rows screened, proceed/discard, and the observed
precision with a **Wilson 95% interval**. Plus the release's pile composition and
the `pending` split (`no_filter_matched` vs `no_text`), which is the bundle's
coverage gap.

```bash
python -m analysis.rule_report                                  # terminal table
python -m analysis.rule_report --html redesign/rule_report.html # publishable page
python -m analysis.rule_report --json cache/rule_report.json    # raw numbers
```

The store is opened **read-only**; a `python -m filter.engine route` holding the
write lock produces a message saying so rather than a hang. `--release` pins a
release (default: the newest in the store), `--workers` sizes the pool scan, and
the elapsed time is printed on every run.

A rule that has **not been screened yet** prints `not screened`; a rule with no
verdicts anywhere prints `no verdicts`; only a rule that was screened and
proceeded nothing prints `0%`. The same optimism caveat as `arm_evidence` applies
to every label-derived column, and to the screening columns in particular.

Not wired into the Stage 4 dashboard, which is read-only monitoring over
`data/dashboard/` (`shared/dashboard_cache.py`) and registers three blueprints
only. A future "rules" tab would be fed from this script's `--json` output — the
same dict `render()` and `render_html()` print, with `rules[]` one entry per spec
and top-level `piles` / `pending` / `flora` / `negatives` / `screen` blocks —
written on a schedule beside `data/dashboard/stats.json`. Adding the tab is the
maintainer's call, not this script's.

## One-off analyses (historical)

| File | What it did |
| --- | --- |
| `apa_resolver.py` | Resolves replications that have no DOI via CrossRef (title + authors + year), with a manual `apa_reference_fallback.csv` tier, and formats APA references. Reads `all_replications.csv`. |
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
- **`rescan_impact_report.py`** (2026-08-06). It measured the journal-hint branch
  of Stage 3's citation scoring, which was deleted in the cleanup round after zero
  observed firings in production — the script's mock target went with it.
- **`rule_analysis.py`** and its `extraction_audit.md` output. It broke
  `extracted.csv` down by link method, confidence and missing `doi_o`. The
  read-only audit over the current export is `extract/audit_extracted.py`, which
  flags the same fields per row.
- **`purge_offpile.py`** (2026-08). It moved the pre-engine corpus out of
  `extracted.csv` and its set-aside CSVs into `data/legacy_pre_engine/`, once, after
  the #146 handoff replaced Stage 3's input. The move is done.
- **`rescan_stage3_relink_impact.csv`** — a before/after `doi_o` relink table
  (`old_doi_o` / `new_doi_o` / `resolution_method` per `doi_r`) from the 2026-08
  rescan. Its sibling `rescan_filter_gate_impact.csv` is in `archive/`.

The **superseded eval run records** are not deleted but moved: the per-prompt,
per-model `voter_*.json` and `pre_p*.json` files, the pre-v3.3 screening prompts
and their scoring scripts, and the Stage 3 rendered payloads now live under
`archive/`, paths mirrored. What each one is and why it moved is in
[`archive/README.md`](../archive/README.md); the reports that cite their numbers
stay here.

## Running one

Every script that has a CLI is a module: `python -m analysis.<name>`, with `--help`
on each. `apa_resolver.py` is the exception — it has no `__main__` block and is
imported rather than run.

Inputs come from `shared.config.DATA_DIR`, logging from `shared.config.log`.
Requires `pandas` and `requests` (plus `fuzzywuzzy` for `apa_resolver`'s optional
fuzzy tier). `analysis/citation_gate_out/` is gitignored.
