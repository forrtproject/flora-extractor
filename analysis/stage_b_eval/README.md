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

## Caveat on the gold corpus: `allrep_llm` is not ground truth

Every "gold positive" number in these reports is measured against the 7,505-paper
`override_positives.json`, which draws from four sources — and **4,681 of those papers
(62%) come from the `allrep_llm` bucket: the old pipeline's own LLM verdicts, never
human-confirmed.** Human curation (`entry_sheet` 1,551, `flora_db` 1,266) is 38%.

Auditing one slice (the 59 genetics papers the keyword filter rejects, see
`gwas_scope_classification.csv`) found 14 rows that fail FLoRA's own coding rule — 6
two-stage internal designs, 8 molecular-sense papers — and **all 14 came from
`allrep_llm`**. Human-curated rows made none of those errors.

So "recall against gold" is, for the majority of the corpus, agreement with a superseded
LLM prompt. Treat every recall figure here (the 443 negatives / 5.9%, the 319 vocabulary
misses, the 121 exclusion kills) as an upper bound on the real miss count, and restrict
to the human-curated buckets — or report the two strata separately — before tuning
filter rules on these numbers.
