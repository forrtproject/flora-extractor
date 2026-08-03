# Stage B evaluation artifacts (issue #144)

Evidence base for the staged-waves design in issue #144, produced 2026-08-03 on
commit `3c12ba3` (feat/keyword-ladder) using production gate/verdict code imported
from that checkout. All snapshot measurements use real parquet partitions from the
2,446-file OpenAlex manifest, sampled by deterministic stride.

| File | What it is |
| ---- | ---------- |
| `vocab_holes_report.md` / `vocab_candidates.csv` | The 319 no-phrase FLoRA gold misses: reachable-vs-invisible split, categories, and vocabulary candidates each priced as gold recovered vs. extra admissions per million rows (measured on 2.9M snapshot rows) |
| `exclusion_narrowing_report.md` / `exclusion_candidates.csv` | The 121 exclusion-killed gold positives: per-pattern protective value (census of all 3,785 exclusion-firing survivors within 5.6M sampled rows) and two-sided narrowing candidates, incl. proposed YAML diffs |
| `reproduction_coverage_report.md` / `repro_vocab_candidates.csv` | The 93-report reproduction entry sheet vs. the pipeline: OpenAlex indexing, gate/verdict coverage, the five losses, and why reproduction vocabulary work is not needed |
| `pilot_sample_positive.csv` / `pilot_sample_ambiguous.csv` / `pilot_sample_concept.csv` | 1,000-row random samples per admission arm, reservoir-sampled (seed 20260803) from 40 partitions / 10,943,785 scanned rows |
| `pilot_sampling_notes.md` / `pilot_summary.json` | Sampling design, per-arm population counts and descriptives, known-gold hit rates |

The pilot samples feed the pre-commitment screen pilot described in issue #144
(screen all three arms, human-label random samples of proceeders AND discards).
