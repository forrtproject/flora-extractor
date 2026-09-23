# Why `cannot_be_determined` is over-used (handover step 6, 2026-09-23)

Everything here is reproducible from the scripts in this folder; bulky outputs are
gitignored locally. Nothing in `shared/` or `extract/` was changed by the investigation.

## Rates (replication rows of `data/extracted.csv`, read with pandas)

686 of 3,896 rows are cbd (17.6%).

| Split | cbd rate |
|---|---|
| abstract, no document | 13.1% |
| abstract + document | 24.1% |
| no abstract + document | 22.9% |
| **non-OSF**, no document | 11.7% |
| **non-OSF**, with document | 12.8% |
| **OSF** records (`10.17605` or an `osf*` source) | 52% (282 of the 686 cbd rows) |

By link method: `llm_author_year_search` 39%, `llm_title_search` 20%, `llm_references` 11%
(9% without a document, 37% with one). By source: `osf_files` 55%, `osf_registration` 61%;
by parser `grobid` 67%, `docx` 54%.

## Why a document seems to raise the rate

1. **OSF plans.** Most OSF cbd rows are preregistrations / Stage 1 plans deposited as
   projects. gpt-5.6-luna marked them `completed`, so they fell to cbd; gpt-6-luna with the
   unchanged prompt marks 10 of 14 sampled ones `prospective` (→ `prospective_registration.csv`).
2. **Selection.** Only rows the abstract / reference rungs could not settle go on to fetch a
   document, so rows with a document are the hard ones.
3. **The full-text call never sees the results section.** It gets the first 2,000 chars of
   the introduction; a METHODS block that is always empty (`sections["methods"] = ""` is
   hard-coded); and 6,000 chars of "discussion" from `outcome_text`. 54% of cached full-text
   prompts (2,820 / 5,215) found no discussion heading and sent the last 6,000 chars before
   the references instead. Non-OSF cbd is 29% when that happens, 9% when a heading is found.
   In 6 of the 10 judged cbd rows with a document, the passage the judges used sat in the
   results/body (positions 0.50–0.77) and was not sent.
4. **A bug that discards settled answers.** When the link was accepted earlier and the
   full-text call codes that same original as settled but cannot key it (`key: null`), its
   outcome is dropped and the earlier cbd ships. 41 rows (22 `llm_references`); cbd-001 confirmed.

The standalone coder (intro + discussion, no results) has the same gap but only codes
rule-picked links, which are 0.4% cbd.

## Causes by sample

| Cause | 19 judged | 26 non-OSF with doc | 14 OSF with doc | 20 no doc |
|---|---|---|---|---|
| Evidence not sent (results section, empty parse, no document) | 6 | 8 | 1 | 0 |
| Prompt too strict (result in the text; model wanted the original's finding restated or an overall verdict) | 6 | 4 | 0 | 8 |
| Discard bug | 1 | 2 | 1 | 0 |
| Several originals, result not attributable | 2 | 4 | 0 | 3 |
| No result exists (plan, erratum, supplement, template) | 0 | 6 | 12 | 1 |
| Genuinely unclear | 4 | 1 | 0 | 7 |
| Settled by re-running on gpt-6-luna alone | 0 | 1 | 0 | 1 |

## The fix and what it did

**Recommended: `fix_link_original.patch`** — send the whole parsed document at the full-text
step for every row (not only multi-target papers), and fix the discard bug. Replayed cached
prompts on gpt-6-luna; number still cbd:

| Group | Shipped | Re-run, unchanged prompt | With the fix |
|---|---|---|---|
| 19 judged | 19 | 12 | 8 |
| judged, with document (10) | 10 | 7 | 3 |
| non-OSF with document (26) | 26 | 16 | 8 |

On 60 settled controls the fix moved no more answers than a plain re-run (3 vs 4); no
settled answer became cbd. Against the judges on the 10 with a document: right on 7, wrong on
2 (successful where they said inconclusive), 1 still cbd. Extra cost ≈ 7.5k input tokens per
full-text row.

**Not recommended yet: `fix_prompts.patch`** (relaxes "the evidence must state what the
original found", adds a check-every-block paragraph). Individually plausible changes, but the
aggregate effect is within run-to-run noise at n≈30, and it moved twice as many settled
controls as a plain re-run (8 vs 4, one turned cbd).

**Projection (rough):** reopening cbd works with the recommended fix takes replication cbd
from 17.6% to ~7–9%, with ~200 OSF rows moving to `prospective_registration.csv`.

## Mixed vs successful

The prompt calls a replication mixed when any result differs from the original — eager about
extensions and side measures (what the 5 judged cases show). An edit excluding both changed
nothing measurable across 70 rows. The model moves these rows: gpt-6-luna with the current
prompt already codes 10 of the 24 disputed mixed→success rows as successful (13% of random
mixed rows). Agree the semantics with Dan before editing the prompt.

## Reopen

The recommended fix is code only: the extract generation does not move, no equivalence needed.
`out/reopen_cbd_only.txt` lists 691 works with a cbd row, excluding the 27 cbd rows already
past `unvalidated` in Supabase (read-only query). Dry run, sandbox (`--mode validation`), then live:

```bash
.venv/bin/python -m extract.tier --redo-status outcome=cannot_be_determined --only "$(cat analysis/cbd_investigation/out/reopen_cbd_only.txt)"
```

It overlaps 196 `llm_references` cbd rows with the step-3 redo — land both fixes first so one
pass fixes both.

## Spend

658 gpt-6-luna calls, ~6.0M in / 1.5M out tokens; flex kept refusing, so standard tier
(`OPENAI_USE_FLEX=false` in that process only): ≈$0.7 at flex price, ≤≈$1.5 at standard.

## Open questions

- OSF projects that only carry a prereg: prospective quarantine, or `not_a_replication`?
- Measure `fix_prompts.patch` on ~150 no-document cbd rows, or drop it?
- Mixed semantics with Dan.
- Not fixed: the dead METHODS block; grobid/playwright parses with no raw text (send the
  abstract only); no `results` value for `out_quote_source`.

## Sandbox check of the committed fix (af85ea6 + ladder 28), 2026-09-23

60 random works from `out/reopen_cbd_only.txt` outside the `llm_references` redo
(`out/sandbox_60.txt`), run `--mode validation`, rendered to `out/sandbox_60.csv`, compared
in `out/sandbox_60_compare.csv`. 58 were extracted (2 skip-listed).

| Ending of the 58 (all had a cbd row live) | works |
|---|--:|
| still has a cbd row | 19 |
| `prospective_registration` (OSF plans; quarantined, not lost) | 25 |
| settled outcome (successful / failed / mixed / partial) | ~8 |
| `target_pending` / `no_original_found` / search-unconfirmed | ~6 |

Works with a cbd row: 60 → 19. Most of the drop is the OSF plans moving to their own
quarantine, as predicted; the titles read as prereg/replication projects. Spend ≈ $0.04.
