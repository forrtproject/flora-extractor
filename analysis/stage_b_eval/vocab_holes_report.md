# The vocabulary hole: what `keyword_verdict` misses on the FLoRA gold corpus, and what it would cost to close it

Measured on commit `3c12ba3` (branch `feat/keyword-ladder`), working tree clean for
`filter/` and `shared/`. All scripts and intermediate JSON live beside this file.
Machine-readable candidate menu: `vocab_candidates.csv`.

---

## 1. The miss set, recomputed

Gold corpus: `.claude/worktrees/issue-130-prescreen/analysis/prescreen_eval/override_positives.json`
— 7,505 FLoRA gold positives with title + abstract. `keyword_verdict(title, abstract)`
was recomputed over all of them (no `year` argument; the corpus carries no year field).

| outcome | n | share |
| --- | ---: | ---: |
| `positive` | 6,921 | 92.2% |
| `ambiguous` | 144 | 1.9% |
| `negative` | **440** | 5.9% |
| — of which killed by an exclusion pattern | 121 | |
| — of which `"no replication phrase detected"` | **319** | |

**Drift flag.** The brief quotes 443 negative / 322 no-phrase from
`issue_screening_ladder.md`. The exclusion count reproduces exactly (121); the
no-phrase count is 319 here, not 322. `keyword_verdict` was introduced in `3c12ba3`
itself and the phrase list is unchanged since `95d7dc3`, so the 3-row difference comes
from the earlier measurement's harness, not from a code change I can point at. I could
not reproduce the 322 and did not chase it further; everything below uses the 319 I
measured.

## 2. Reachable vs invisible — the split that decides whether this work helps Stage A

Stage A's survivor gate (`_gate_masks` in `search/snapshot_scan.py`) is
`REPLICATION_STEM_PATTERN` over the title **or** the raw `abstract_inverted_index`
JSON, OR'd with the concept mask. Stage B is `_admit` = `concept_hit or
keyword_verdict ∈ {positive, ambiguous}`.

| | n | share of 319 |
| --- | ---: | ---: |
| **Reachable** — a replication stem is present, Stage A survives the row, Stage B drops it | **278** | 87.1% |
| **Invisible** — no stem anywhere in title or abstract | **41** | 12.9% |

Of the 278 reachable, **all 278** carry the stem in the *abstract only* — zero carry it
in the title. That is by construction: a title stem routes to `ambiguous`, so a title
stem can never end in this bucket. Every one of these rows is already a Stage A
survivor and already reaches Stage B; the loss is entirely Stage B's phrase list.

Widening the whole gold corpus, not just the misses: **41 of 7,505 gold positives
(0.55%) have no replication stem anywhere.** Stage A's stem gate has 99.45% recall on
the FLoRA corpus. *The vocabulary hole is a Stage B / `keyword_verdict` phrase-coverage
problem, not a Stage A stem problem.*

Every miss has an abstract (0 of 319 are abstract-free) and 318 of 319 are in Latin
script (1 has a substantial non-Latin block — the Japanese-titled
`10.2132/personality.33.3.13`, whose abstract is English). **Neither "no abstract" nor
"non-English" is a real category here** — those rows were presumably already lost
before this corpus was assembled, since the corpus was built by joining gold DOIs to
`data/filtered.csv` rows that carry text.

## 3. Why each miss is missed

Categories are assigned in priority order (first rule that fires wins), so they sum to
319. Derived from reading the stem sentence of every miss (`miss_sentences.txt`), not
from an a-priori vocabulary list.

| n | category | example DOIs |
| ---: | --- | --- |
| 97 | **`we <adverbial> replicate(d)`** — the list has `\bwe replicated\b` and `\bwe \w+ly replicate[sd]?\b` and nothing else, so "we **also** replicated", "we **first** replicate", "we **were able to** replicate", "we **then** replicated" all fall through | `10.1002/bdra.20791`, `10.1002/bsl.2379`, `10.1002/jaba.669`, `10.1002/jocb.350`, `10.1007/s00292-021-00947-4` |
| 63 | **matrix verb + `to replicate`** — the list covers only `attempt*`, `aim*`, `set out`. Missing: sought, seek, tried, try, wanted, intended, hoped, started, designed, planned | `10.1002/bin.1640`, `10.1002/ece3.1549`, `10.1007/s00426-023-01924-7`, `10.1016/j.applanim.2014.02.016`, `10.1016/j.egypro.2016.07.026` |
| 39 | other replication-stem sentence, no compact shape (`I replicate…`, `three studies replicated…`, `attempted replication failed`, `conceptually replicated prior work`) | `10.1002/dys.1497`, `10.1002/jae.2445`, `10.1007/s12310-020-09377-8`, `10.1016/j.asw.2014.10.001`, `10.1016/j.learninstruc.2009.11.003` |
| 30 | **the GWAS phrase guard** kills the only phrase that matched. These are genetics replications FLoRA counts as positives; `_GWAS_GUARD` removes them because the same shape is also the discovery/replication-cohort design | `10.1002/ajmg.b.31098`, `10.1002/cpt.2337`, `10.1002/gepi.22167`, `10.1002/hep4.1751`, `10.1002/oby.20268` |
| 22 | no textual signal at all — findable only by citation or curation | `10.1002/jae.1078`, `10.1002/jae.2446`, `10.1002/jrsm.1298`, `10.1017/s0003055405051658`, `10.1037/a0013838` |
| 17 | passive (`was/were/has been replicated`) | `10.1007/s11145-012-9365-8`, `10.1016/j.jsp.2019.07.014`, `10.1016/j.learninstruc.2019.01.002`, `10.1016/j.ridd.2013.11.006`, `10.1016/j.ridd.2019.103495` |
| 15 | no stem anywhere — economics **Comment / Revisited / Correction** genre | `10.1080/09603107.2011.564130`, `10.1093/pan/mpi019`, `10.1111/j.1540-6261.2012.01744.x`, `10.1162/rest_a_00173`, `10.1257/aer.20120767` |
| 11 | gerund (`replicating X`) | `10.1002/jaba.659`, `10.1007/s10648-020-09553-x`, `10.1007/s11145-017-9723-7`, `10.1016/j.compedu.2013.01.007`, `10.1016/j.learninstruc.2012.08.001` |
| 6 | failure nouns / plurals (`failures to replicate`, `failed replications`, `replication failures`) | `10.1007/s11409-015-9146-2`, `10.1027/1864-9335/a000182`, `10.1089/cyber.2012.0629`, `10.1163/15685373-12342140`, `10.31234/osf.io/r7pd3` |
| 4 | supplemental-material stubs (`*.supp`, boilerplate repository text) — not recoverable and not worth recovering | `10.1037/edu0000965.supp`, `10.1037/xge0001503.supp`, `10.1037/xlm0000874.supp`, `10.1037/xlm0001066.supp` |
| 2 | `replicab*` / replication-cohort framing only | `10.1080/00224499.2021.1999893`, `10.1097/mlr.0000000000002256` |
| 1 | qualifier not in the list (`systematic/partial/independent replication`) | `10.1177/20531680251335651` |

**Alternative vocabulary is not the story.** 274 of the 319 contain `replicat*`/`replicab*`;
14 contain `reproduc*`; 3 contain `reanalys*`/`reanalyz*`. The non-`replicat` words the
brief anticipated appear as incidental co-occurrences, not as the paper's own claim:
`comment/reply` 41, `confirm*` 39, `previously reported/found` 21, `generaliz*` 17,
`meta-analy*` 16, `preregist*` 12, `revisit*` 9, `robustness` 5, `registered report` 2.
The hole is **morphological and syntactic coverage of `replicate` itself**, not a missing
lexical field.

## 4. What each candidate costs

**Snapshot denominator.** 2,892,614 real OpenAlex rows from 10 manifest partitions
(indices 2, 3, 5, 6, 10, 13, 14, 15, 16, 19 of the stride-122 list the gate labs used),
streamed with column projection and materialised locally as `corpus/part-*.parquet`.
Under the current rule these give 36,317 Stage A survivors, **4,523 admissions
(1,564 per million)**, and 31,794 survivors that Stage B currently drops. Partition 13
is a degenerate cluster of fusion-experiment dataset dumps; excluding it (2.49M rows)
moves every per-million figure by under 15% and changes no ranking, so the 2.89M
figures are reported.

**Costing rule.** A candidate's "extra admissions" are rows that today are Stage A
survivors, are *not* admitted, and have *no* exclusion pattern firing (an exclusion
dominates the phrase list, so those rows would not be recovered anyway) — and that the
candidate matches. Gold-negative columns count only rows the current rule already
rejects, i.e. rows a candidate could newly admit; those pools are 74 of 184
`goldneg_screen` and 14 of 400 `goldneg_curated`.

### 4a. `keyword_verdict` phrase list (Stage B admission + Stage 2 filter)

Ranked by recovered gold positives per extra admission per million.

| candidate | gold recovered (of 319) | extra/1M | +% admissions | goldneg screen | goldneg curated | ratio |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| `G1_gwas_guard_exemption` (guard exemption, not a phrase) | 22 | **0.0** | 0.0% | 0 | 1 | ∞ |
| `C13_attempted_replication_noun` | 2 | **0.0** | 0.0% | 0 | 0 | ∞ |
| `C2_we_gap_replicate` `\bwe\s+(?:\w+\s+){0,2}replicat(?:e|es|ed)\b` | **162** | 5.5 | 0.4% | 0 | 6 | **29.3** |
| `C11b_pronoun_gap_replicate_prioranchor` | 36 | 1.4 | 0.1% | 0 | 1 | 26.1 |
| `C2n_we_adverbial_replicate` (closed adverb class) | 78 | 3.1 | 0.2% | 0 | 0 | 25.1 |
| `C12_fail_to_replicate_forms` (`fail*/unable/attempt* to replicate`) | 17 | 0.7 | 0.0% | 0 | 1 | 24.6 |
| `C11_pronoun_gap_replicate` (I/we/they/the authors, 3-word gap) | **200** | 11.8 | 0.8% | 0 | 6 | 17.0 |
| `C1b_matrix_verb_no_able` | 44 | 3.1 | 0.2% | 0 | 0 | 14.2 |
| `C6_failure_nouns` | 7 | 0.7 | 0.0% | 0 | 1 | 10.1 |
| `C10_replication_sample` (control — GWAS cohort noun) | 5 | 0.7 | 0.0% | 0 | 2 | 7.2 |
| `C1_matrix_verb_to_replicate` (incl. `able to`) | 64 | 10.4 | 0.7% | 0 | 0 | 6.2 |
| `C8_widened_object` (widened object noun list) | 52 | 12.4 | 0.8% | 0 | 0 | 4.2 |
| `C7_qualifier_replications` | 2 | 1.4 | 0.1% | 0 | 1 | 1.4 |
| `C3_thirdperson_subject_replicate` | 14 | 15.9 | 1.0% | 0 | 0 | 0.9 |
| `C4c_passive_no_modal` (passive minus recommendation/biology sentences) | 15 | 20.1 | 1.3% | 0 | 2 | 0.7 |
| `C4_passive_replicated` | 29 | 89.2 | 5.7% | 0 | 2 | 0.3 |
| `C5_replicating` (bare gerund) | 17 | 58.4 | 3.7% | 0 | 1 | 0.3 |
| `C9_replicab` | 4 | 69.8 | 4.5% | 0 | 0 | 0.06 |
| `A1_abstract_stem_to_ambiguous` (**maximal** option) | **278** | 10,819 | **+692%** | 74 | 13 | 0.03 |

Reading the losers (`refined_examples.json`, `snapshot_examples.json` hold sampled
match windows):

- `C4_passive_replicated` noise is "this research **should be replicated** in larger
  samples" (a future-work recommendation) and "each treatment **was replicated** 22
  times" (a biological/experimental replicate). Vetoing modal and count-noun sentences
  (`C4c`) halves the cost but also halves the recall; the ratio stays under 1.
- `C5_replicating` noise is the virus/artifact sense: "key roles in **replicating** the
  virus", "**replicating** artifacts on campus in 3D models".
- `C9_replicab` noise is methodological praise: "a **replicable** learning model",
  "provides superior sensitivity and **replicability**".
- `C1`'s extra cost over `C1b` is entirely the `able to replicate` arm — "a prosthetic
  foot **able to replicate** the function of the biological foot". Dropping `able`
  keeps 44 of 64 recovered positives at 30% of the cost.
- `A1` (treat any abstract stem as `ambiguous`) recovers all 278 reachable misses but
  admits 10,819 extra rows per million — an 8× increase in the Stage 1 corpus — and
  newly admits 74 of the 74 currently-rejected `goldneg_screen` rows and 13 of 14
  `goldneg_curated`. It is the reference point for "vocabulary is not free", not a
  proposal.

**The cite gate does not work here.** Requiring a same-sentence author-year citation
alongside the candidate phrase (the discriminator `keyword_verdict` already uses for the
#44 rescue) collapses recall to 0–3 papers for every candidate tried. FLoRA abstracts
overwhelmingly say "we replicated the effect", not "we replicated Smith (2010)".

### 4b. Bundles (unions, measured as unions — overlaps removed)

| bundle | members | recovered | extra/1M | +% admissions | goldneg screen | goldneg curated | ratio |
| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: |
| **T1 minimal** | G1 + C13 + C6 + C12 + C1b | 87 (27%) | 4.5 | +0.3% | 0 | 2 | 19.4 |
| **T2 core** | T1 + C2 | **186 (58%)** | **9.3** | **+0.6%** | 0 | 7 | **19.9** |
| T3 wide | G1 + C13 + C6 + C12 + C1 + C11 + C8 + C7 | 229 (72%) | 34.6 | +2.2% | 0 | 7 | 6.6 |
| T4 wide + passive/gerund/3rd-person | T3 + C4c + C5 + C3 | 251 (79%) | 119.6 | +7.6% | 0 | 8 | 2.1 |

### 4c. Stage A stem gate (`REPLICATION_STEM_PATTERN`)

Only the 41 stem-invisible gold positives can be gained here. "Extra survivors" are rows
the stem gate does not see today; "extra admissions" assumes the new stem is also routed
through the title-stem `ambiguous` arm (a new stem that only enters the Stage A mask
admits **nothing** — measured `admitted-today = 0` for every candidate, because
`keyword_verdict` would still return `negative`).

| candidate | invisible gold reached (of 41) | extra Stage A survivors/1M | extra admissions/1M | goldneg screen | goldneg curated | ratio |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| `S8_comment_genre` (`: Comment`, `Comment on "X"`) | 7 | 714 | 414 | 2 | 3 | 0.02 |
| `S1_revisit` | 6 | 682 | 346 | 2 | 3 | 0.02 |
| `S9_revisited_suffix` | 4 | 279 | 199 | 1 | 0 | 0.02 |
| `S5_reappraisal` (`re-appraisal/assess/evaluat/estimat`) | 3 | 510 | 144 | 1 | 4 | 0.02 |
| `S4_correction_to` | 2 | 144 | 19 | 0 | 2 | 0.10 |
| `S6_robustness` | 2 | 1,058 | 101 | 2 | 7 | 0.02 |
| `S2_reexamin` | 1 | 246 | 53 | 2 | 1 | 0.02 |
| `S3_comment_on` | 1 | 837 | 482 | 2 | 3 | 0.00 |
| `S7_reanalyz_all` | 0 | 0 | 0 | 0 | 2 | — |

Every Stage A stem candidate is 2–3 orders of magnitude worse value than the phrase
candidates, and `S6_robustness` alone would hit 7 of the 14 currently-rejected
`goldneg_curated` rows. **No Stage A stem addition is worth making for this population.**
`S7_reanalyz_all` confirms the current stem already covers every `reanaly[sz]` form.

## 5. Recommendation

1. **Take T2 core.** 186 of 319 misses (58%), +0.6% admissions (9.3 rows per million
   scanned), zero `goldneg_screen` hits. Its two big pieces are the two the gold data
   actually names: allowing up to two words between `we` and `replicate(d)`, and the
   sought/tried/wanted family before `to replicate`.
2. **`G1` first, on its own merits.** Exempting the GWAS guard when the sentence names a
   prior report recovers 22 gold positives and admits **zero** extra rows in 2.89M. It
   is the only free item on the menu. It does need a decision the data cannot make: the
   30 guard-killed papers are genuine GWAS replication-cohort designs that FLoRA counts
   as positives, so this is a scope call, not a precision fix.
3. **Do not take the passive, gerund, `replicab*` or bare third-person arms.** Their
   noise is the biological-replicate and future-work senses, which have no compact
   shape; T4's extra 22 papers cost 13× T2's admissions.
4. **Leave the Stage A stem gate alone.** 41 of 7,505 gold positives (0.55%) are stemless
   and no stem addition recovers them at a defensible price. The economics
   Comment/Revisited genre in particular is a curation or citation problem.
5. **Ceiling.** 26 of the 319 (22 signal-free + 4 `.supp` stubs) carry nothing a keyword
   rule could use. The reachable ceiling for phrase work is 293; T2 reaches 63% of it,
   T4 86%.

## 6. What I could not verify

- The 322 → 319 drift (§1).
- Whether the 41 stem-invisible papers would be admitted by Stage A's **concept** mask.
  `override_positives.json` carries no concept list and I made no API calls, so the
  "invisible to Stage A entirely" label is accurate for the token gate only.
- Precision of the extra admissions is judged from sampled match windows
  (`refined_examples.json`, `snapshot_examples.json`, 10–25 per candidate), not from
  labelled review. The gold-negative columns are the only labelled FP evidence and both
  sets are small (74 and 14 rows at stake).
- The gold corpus is FLoRA's curated positives joined to `data/filtered.csv`; it is not a
  random sample of replications, so "recovered" is recall against FLoRA's own scope.

## 7. Files

| file | what |
| --- | --- |
| `vocab_candidates.csv` | the full machine-readable menu (40 rows: 14 phrase candidates, 4 refinements, 7 cite-gated variants, 9 stem candidates, 2 whole-arm options, 4 bundles) |
| `step1_misses.py` → `misses.json` | recompute + isolate the 319 |
| `step3_chars.py`, `step3b_sents.py` → `miss_sentences.txt` | stem-sentence dump behind §3 |
| `step3c_why.py` → `guard_killed.json`, `nopattern.json` | guard-killed vs no-pattern split |
| `step4_categories.py` → `categories.json` | the §3 table |
| `candidates.py` | every candidate pattern |
| `build_corpus.py` → `corpus/` | 2.89M real snapshot rows, reduced |
| `eval_gold.py`, `eval_snapshot.py`, `eval_refined.py`, `eval_bundles.py` | the four measurements |
