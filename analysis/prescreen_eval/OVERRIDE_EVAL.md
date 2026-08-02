# The pre-screen override, measured on 7,505 FLoRA papers

`hard_signal()` in `shared/prescreen.py` is the deterministic bypass in front of the
cheap pre-screen: when the title+abstract states the replication design outright, no
small model gets to discard the row. Issue #130 found it load-bearing — it caught all
four cases where both cheap voters jointly missed a gold positive — but its 17 patterns
were written *after* reading those four misses, so a perfect score on the 567 rows that
motivated them proves little.

This is the override run against a corpus 13× larger and independent of that derivation.

Everything below comes from `build_override_sets.py` and `eval_override.py`, both
rerunnable; no LLM calls are involved anywhere in this evaluation.

## The two corpora

`override_positives.json` — **7,505 FLoRA replication/reproduction papers**, deduplicated
on `clean_doi`, most trusted source first:

| bucket | n | source |
| --- | --: | --- |
| `flora_db` | 1,266 | `data/flora.csv` — the curated FLoRA database |
| `entry_sheet` | 1,551 | `data/flora_entry_sheet.csv`, minus `validated - discarded` |
| `reproductions` | 7 | `data/reproductions.csv` (cp1252) |
| `allrep_in_flora` | 0 | `all_replications.csv`, `already_in_flora` — every one already claimed above |
| `allrep_llm` | 4,681 | `all_replications.csv`, `llm_confirmed`, `type` ∈ {replication, reproduction} |

Titles come from `filtered.csv` where the row exists there (7,408 of 7,505) and otherwise
from `flora.csv`'s `title_r` or the APA `ref_r`; abstracts come from the curated file
first, `filtered.csv` second. `needs_review` rows of `all_replications.csv` are excluded —
nobody has confirmed they are replications.

Dropped before scoring: 8 with no title, 250 with no abstract anywhere, and **1,010 whose
abstract is under `PRESCREEN_MIN_ABSTRACT_CHARS` (200)**. That last exclusion matters:
production bypasses the pre-screen on those rows via `short_text`, so the override never
decides them. Every recall number here is therefore *conditional on the paper having a
usable abstract*, and papers with usable abstracts are the well-indexed ones — the
override's real-world recall over all of FLoRA is not measured by this and cannot be.

`override_negatives.json` — **1,333 non-replications that genuinely reach Stage 3**:

| bucket | n | source |
| --- | --: | --- |
| `screen_discard` | 183 | `not_a_replication.csv`, `link_method == not_a_replication` — the current, validated Stage 3 screen's own discards |
| `curated_false_positive` | 1,150 | `all_replications.csv` `validation_status == false_positive`, kept only where `filtered.csv` has the row with `filter_status != false_positive` |

The ~5,000 cap never bound: of 14,148 old-pipeline false positives, **12,919 are killed by
Stage 2** and never reach Stage 3 at all, which is what leaves 1,150. Both corpora were
built in one streaming pass over `data/filtered.csv` (2,581,092 rows).

The two negative buckets behave very differently and are reported separately throughout.
`screen_discard` is the honest one — same pipeline, same population, current prompt.
`curated_false_positive` is LLM-labelled and drawn from an old *replication-keyword*
harvest, so it is saturated with replication vocabulary in non-replication senses (viral
replication, DNA replication, "replication of the model") and gives a pessimistic bound.

**Dollar convention.** One needless override fire costs one validated-screen call,
≈ $0.0018. `$/pass` = hit rate on `screen_discard` × 49,800 rows reaching Stage 3 per
corpus pass × $0.0018. Screening the entire Stage 3 population at full price is ~$90/pass,
so that is the ceiling any of these figures is measured against. A missed positive, by
contrast, costs a paper — permanently, since a pre-screen discard is terminal.

## What the shipped override does

```
positives           : 7,505
  override fires    : 5,999 ( 79.9%)
  override misses   : 1,506
negatives           : 1,333
  override fires    : 894 ( 67.1%)
  screen_discard         56/183    30.6%   = $27/corpus pass
  curated_false_positive 838/1,150 72.9%
```

Recall by bucket — the human-curated buckets score highest, the LLM-confirmed ones lowest:

| bucket | recall |
| --- | --- |
| `flora_db` | 1,073/1,266 = 84.8% |
| `entry_sheet` | 1,298/1,551 = 83.7% |
| `allrep_llm` | 3,624/4,681 = 77.4% |
| `reproductions` | 4/7 = 57.1% |

So the override's measured perfection on the 567-row set does not survive contact with a
larger corpus: **one FLoRA paper in five states its design in terms the regex does not
recognise.** That is the headline. On the other side, it already fires on 31% of the
current screen's own discards, i.e. ~$27 of the ~$90/pass the pre-screen exists to save.

### Per pattern

`pos hit` = share of the 7,505 positives matched; `unique` = positives matched by this
pattern and no other (its true recall contribution); the two negative columns are hit
rates on the two negative buckets.

| # | pattern (abbreviated) | pos hit | unique | neg screen | neg curated | $/pass |
| --: | --- | --: | --: | --: | --: | --: |
| 0 | `replication study/attempt/experiment` | 19.0% | 508 | 1.1% | 4.7% | 1 |
| 1 | `direct\|close\|conceptual\|… replication` | 16.8% | 378 | 3.8% | 8.3% | 3 |
| 2 | `registered replication report` | 0.5% | 0 | 0.0% | 0.1% | 0 |
| 3 | `replication of (a\|the\|…\|X's)` | 15.3% | 578 | 25.7% | 20.7% | 23 |
| 4 | `replication and extension` | 8.7% | 333 | 0.0% | 1.5% | 0 |
| 5 | `re-test the original/earlier` | 0.0% | 1 | 0.0% | 0.0% | 0 |
| 6 | `we … replicate(d)` | 34.0% | 1,296 | 0.5% | 35.6% | 0 |
| 7 | `aim/attempt/sought … to replicate` | 14.4% | 310 | 0.5% | 1.2% | 0 |
| 8 | `failed/succeeded to replicate` | 5.2% | 111 | 0.0% | 3.9% | 0 |
| 9 | `successfully/not replicated` | 3.7% | 69 | 0.5% | 5.4% | 0 |
| 10 | `replicated the original/finding/…` | 4.4% | 33 | 0.5% | 8.3% | 0 |
| 11 | `reproduce the results of/from` | 0.3% | 5 | 1.6% | 0.2% | 1 |
| 12 | `computational reproduction/ibility` | 0.1% | 2 | 0.0% | 0.0% | 0 |
| 13 | `reproducibility project/check/audit` | 0.5% | 0 | 0.0% | 0.4% | 0 |
| 14 | `re-analysis of the original/published` | 0.0% | 0 | 0.0% | 0.0% | 0 |
| 15 | `many labs` | 0.3% | 0 | 0.0% | 0.1% | 0 |
| 16 | `reproducibility project` | 0.5% | 0 | 0.0% | 0.4% | 0 |

Three things fall out of this table:

* **Five patterns do all the work.** #6, #0, #1, #3 and #4 contribute 3,093 of the unique
  matches between them. Patterns #2, #13, #14, #15 and #16 contribute **zero unique
  matches** — every paper they catch is caught by something else. They are free to keep
  and free to drop.
* **Pattern #3 is where the money goes**: 25.7% of screen-confirmed negatives, $23 of the
  $27/pass the override currently costs, for 578 unique positives. It is the only shipped
  pattern with a meaningful price.
* **The reproduction/re-analysis vocabulary is nearly dead.** Patterns #11–#14 match 0.3%,
  0.1%, 0.5% and 0.0% of positives. `re-analysis of the original/published` matches **one
  paper in 7,505**. This part of the override was written from the vocabulary reproduction
  papers *ought* to use; the corpus says they mostly write "this study replicates" like
  everyone else, and the 7 curated reproductions score 4/7.

## Proposed additions

The 1,506 missed positives were split by `md5(case id) % 2`, the same rule
`score_prescreen.py` uses. Candidates were derived by reading only the **dev** half (765
cases) — a keyword-context frequency count plus manual inspection of 45 of them — and are
reported on the **test** half (741) they were never shown.

Cost columns count only negatives the shipped override does **not** already fire on
(127 `screen_discard`, 312 `curated_false_positive`), since a row it already bypasses
cannot be charged twice.

| tier | name | dev | **HELD-OUT** | neg screen | neg curated | $/pass |
| --- | --- | --: | --: | --: | --: | --: |
| A | `study_replicates` | 31.5% | **27.8%** | 2.4% | 2.2% | 2 |
| A | `replicate_and_extend` | 26.9% | **23.5%** | 0.0% | 1.6% | 0 |
| A | `replicating_any` | 11.1% | **12.4%** | 0.0% | 5.8% | 0 |
| A | `repl_of_authoryear` | 8.8% | **11.9%** | 0.8% | 4.8% | 1 |
| A | `replicate_previous` | 8.9% | **9.7%** | 0.0% | 0.3% | 0 |
| A | `adj_replication` | 6.8% | **7.0%** | 0.0% | 2.2% | 0 |
| A | `results_replicate` | 7.1% | **5.9%** | 0.0% | 1.3% | 0 |
| A | `replicate_findings` | 7.3% | **5.9%** | 0.0% | 2.6% | 0 |
| A | `aim_replication` | 5.9% | **4.5%** | 0.0% | 1.9% | 0 |
| A | `not_replicate` | 3.3% | **4.0%** | 0.0% | 8.7% | 0 |
| A | `repl_of_prior` | 3.7% | **2.8%** | 0.8% | 3.8% | 1 |
| A | `failures_to_replicate` | 1.7% | **2.0%** | 0.0% | 1.0% | 0 |
| A | `replication_cohort` | 2.9% | **1.1%** | 3.1% | 2.9% | 3 |
| A | `x_and_replication` | 1.2% | **1.1%** | 3.1% | 3.8% | 3 |
| A | `replicated_in_sample` | 1.8% | **0.9%** | 3.1% | 2.6% | 3 |
| A | `reproduce_their_results` | 0.7% | **0.4%** | 1.6% | 0.0% | 1 |
| B | `repl_of_object` | 11.6% | **11.3%** | 7.9% | 20.2% | 7 |
| C | `repl_of_any` (bare `replication of`) | 37.3% | **44.4%** | 49.6% | 87.8% | 44 |

The held-out column tracks the dev column closely for every candidate (the largest
divergence is `replication_cohort`, 2.9% → 1.1%), so these are not overfitted phrases —
they are the vocabulary FLoRA papers actually use, and the shipped override simply
does not cover it.

Combined, on the held-out half only:

| added | held-out misses caught | override recall on ALL held-out positives | neg screen | neg curated | $/pass |
| --- | --: | --: | --: | --: | --: |
| — (shipped) | — | 80.1% | 30.6% | 72.9% | 27 |
| tier A | 70.4% | **94.1%** | +7.9% | +34.3% | +7 |
| tier A+B | 76.0% | **95.2%** | +14.2% | +46.5% | +13 |
| tier A+B+C | 91.9% | **98.4%** | +51.2% | +99.0% | +46 |

## Recommendation

**Add tier A — all sixteen patterns.** (Done: they ship.) It moves held-out override
recall from 80.1% to
94.1% for **$7 per corpus pass** (30.6% → 36.1% of screen-confirmed negatives, $27 → $32).
That is 8% of the $90 gross screening bill, and — since `REPORT.md` puts the tier's net
saving at ~$30/pass on the current population — roughly a quarter of what the pre-screen
currently earns. Both sides scale linearly with the population, so that ratio holds
whatever #129 does to the row count. What it buys is 70% of the gap through which a cheap model can terminally discard a real replication. Even the
pessimistic negative bucket only moves 72.9% → 82.2%.

Within tier A, four patterns carry it — `study_replicates` (27.8% of held-out misses),
`replicate_and_extend` (23.5%), `replicating_any` (12.4%), `repl_of_authoryear` (11.9%) —
and the last four (`replication_cohort`, `x_and_replication`, `replicated_in_sample`,
`reproduce_their_results`) are the only ones whose cost is visible at all, at ~1% of held-
out misses each for $3. Keep them anyway: the asymmetry is $3 against a paper.

**Tier B (`repl_of_object`) is a defensible add**, +1.1 points of recall for $6 more. It is
the marginal call in this whole analysis; taking it is consistent with "bias toward
inclusion", skipping it costs about 1 paper in 90.

**Do not add tier C, bare `replication of`.** It is the largest single gain available
(+44.4% of held-out misses, recall to 98.4%) and it costs $44/pass — half the entire
Stage 3 screening budget — because "replication of DNA", "replication of HIV" and
"replication of the model" are the same eleven characters. It fires on 99% of the
pessimistic negatives and half of the screen-confirmed ones; at that hit rate the
pre-screen has been disabled by regex rather than configured. If the missing 4% of recall
is judged worth buying later, buy it with a narrower object list (tier B is that, done
properly), not with the bare phrase.

Also worth doing while the file is open: shipped patterns #2, #13, #14, #15 and #16 have
zero unique matches. They are harmless, but #14 (`re-analysis of the original/published`)
matching one paper in 7,505 is a signal that the re-analysis vocabulary in the override is
imagined rather than observed, and should be rewritten from data if reproductions matter.

## What did not pay off

* **Non-English abstracts: nothing to find.** Searching the whole positive corpus for
  `replicación`, `réplication`, `Replikation`, `riproduzione`, `reprodução`, CJK
  equivalents and friends returns **4 papers out of 7,505**, and **0** of the 765 dev-half
  misses. `filtered.csv` abstracts are English; a non-English paper reaches Stage 3 with an
  English abstract or with none at all (and with none, `short_text` bypasses it anyway).
  This avenue is closed, not unexplored.
* **Late-position phrases needed no special handling.** `hard_signal()` searches the whole
  title+abstract with no positional constraint, so a design statement in the last sentence
  already matches. The misses are vocabulary misses, not position misses.
* **21 of the 765 dev-half misses (and 43 of all 7,505 positives) contain no
  replication-family word at all** — no `replicat*`, `reproduc*`, `re-analy*`. Papers like
  "Hand preference for bimanual feeding in captive gorillas: Extension in a second colony"
  are replications that never say so. No regex reaches them; only the LLM screen can, which
  is precisely why the pre-screen's AND gate and the second voter still matter. After tier
  A, 180 of 765 dev misses remain, so ~88% of the residual is still vocabulary a future
  pattern could catch — this is not the last word.

## Caveats

1. **Recall is conditional on having a ≥200-char abstract.** 1,010 positives (12%) were
   excluded for that reason. They bypass the pre-screen in production regardless, so the
   exclusion matches behaviour — but it also means these 7,505 papers are the well-indexed,
   well-abstracted end of FLoRA.
2. **`allrep_llm` (62% of the positive corpus) is LLM-labelled**, not human-verified. Its
   recall is 7 points below the human-curated buckets, which may mean the regex is worse on
   them or that some of them are not replications. Recall on `flora_db` alone (84.8%) is the
   number to quote if only human labels are trusted.
3. **`screen_discard` has n=183.** Every `$/pass` figure inherits that sampling error; a
   single case is 0.5 percentage points, i.e. ±$0.5/pass. The $7 for tier A is a small
   number computed from 10 rows.
4. **49,800 rows/pass** is the Stage 3 population measured on `filtered.csv` on 2026-08-02,
   before any snapshot scanner. Every dollar figure scales linearly with it.
5. **The proposals are derived from data the shipped override already failed on**, so they
   inherit its blind spots. The held-out split protects against overfitting the specific
   phrasings; it does not protect against a whole class of replication vocabulary being
   absent from FLoRA's curated lists.

## Reproducing

```bash
.venv/bin/python analysis/prescreen_eval/build_override_sets.py     # ~6 min, one pass over filtered.csv
.venv/bin/python analysis/prescreen_eval/eval_override.py           # the tables above
.venv/bin/python analysis/prescreen_eval/eval_override.py --misses=45   # dev-half misses, to mine further
```

**Tier A is shipped**: the sixteen patterns are in `_SIGNAL_PATTERNS` in
`shared/prescreen.py`, and `hard_signal()` now fires on 94.7% of the 7,505 positives and
36.1% of the screen-confirmed negatives — the table above, reproduced by the live code.
Tiers B and C remain in `PROPOSED` in `eval_override.py`, unshipped, so re-running the
script still prices them.
