# Can the snapshot pipeline find reproductions?

Measured 2026-08-03 on branch `feat/keyword-ladder`, read-only. Nothing in the repo or
the Google Sheet was modified. All gate/verdict numbers come from the production code
(`filter.phrase_detection.REPLICATION_STEM_PATTERN`, `keyword_verdict`,
`filter.rule_filter.classify_row`, `search.snapshot_scan._admit`), not from a re-implementation.

**Headline: vocabulary is not the binding constraint. Indexing is.** 89% of the gold
reproduction corpus is in OpenAlex, and of what is in OpenAlex the pipeline already
admits 99% at Stage 1 and keeps 94% through Stage 2. The losses are (a) 10% of reports
that OpenAlex has never heard of, and (b) three exclusion-pattern misfires that fire on
the very words a computational reproduction uses.

---

## 1. The corpus is far smaller than expected

The `reproduction list` tab (gid 984458430) has a 4,348-row **grid**, but only
**157 populated data rows**. The 4,347 figure in the brief is the grid size, not the
data.

| | n |
| --- | ---: |
| Sheet data rows | 157 |
| Distinct on (doi_r, url_r, ref_r) | 102 |
| Distinct **reports** (further collapsing rows that share a `doi_r`) | **93** |
| — with a `doi_r` | 86 (92%) |
| — with no `doi_r`, only a URL | 4 |
| — with neither `doi_r` nor `url_r` | 3 |
| — with a usable `abstract_r` in the sheet (>50 chars) | 95 of 102 dedup rows |

The 157→93 collapse is expected: one reproduction report covers several original papers
or several studies, and the sheet gives each original its own row.

`url_r` hosts: doi.org 74, (none) 13, osf.io 6, openalex.org 2, then one each of
econjwatch.org, eprints.lse.ac.uk, econstor.eu, arxiv.org,
replications.clearerthinking.org, shs.hal.science.

Outcome distribution (`outcome_computation`, 102 dedup rows): computationally
reproducible 80, computational issues 16, not checked 5, failed 1.

**Caveat on representativeness.** Only 3 of the 93 reports name I4R in their reference
string. This sheet is a mixed reproduction list (economics comments, Econ Journal Watch,
re-analyses) — it is **not** an I4R corpus. Conclusions about I4R coverage below rest on
those 3 plus the OSF-hosted stratum, not on a large I4R sample.

## 2. OpenAlex indexing

Batch DOI lookups (`filter=doi:A|B|C`, 40/call) plus a title-search fallback from `ref_r`.

| | n | share |
| --- | ---: | ---: |
| Distinct reports | 93 | |
| **Indexed in OpenAlex** | **84** | **90.3%** |
| — resolved by DOI | 83 | |
| — resolved by title search | 1 | |
| **Not in OpenAlex** | **9** | **9.7%** |

Of the 86 reports carrying a `doi_r`, **84 (97.7%)** are indexed. Of the 7 reports with
no `doi_r` at all, **0 are indexed**. The DOI column is very nearly a perfect predictor
of OpenAlex presence — the title-search stratum recovers essentially nothing, so the
brief's step 3 (a 200-row title-search sample) collapses to a 7-row census with a 0% hit
rate.

Title search is also actively dangerous here: 4 of 5 "hits" were the **original** paper,
not the report, because an I4R report's reference string quotes the original's title
(`A comment on Combs et al. (2023) "Reducing political polarization…"`). Those were
inspected and discarded by hand.

Indexed works: type = article 63, preprint 9, report 4, conference-paper 3, book 1,
erratum 1, review 1, editorial 1, other 1. Language = en 79, null 5. OpenAlex has an
abstract for 83 of 84.

## 3. Gate and verdict coverage

Run on the OpenAlex title + reconstructed abstract, i.e. exactly what the pipeline sees.
Stage A = `REPLICATION_STEM_PATTERN` against the title or the raw
`abstract_inverted_index` JSON; Stage B = `keyword_verdict`; admission =
`concept_hit or verdict in {positive, ambiguous}`.

| Stage | n of 84 indexed | share |
| --- | ---: | ---: |
| Stage A stem gate pass | 83 | 98.8% |
| — via title stem | 76 | |
| — via abstract stem | 73 | |
| OpenAlex concept hit (C12590798 / C9893847) | 56 | 66.7% |
| Stage B `positive` | 74 | 88.1% |
| Stage B `ambiguous` | 6 | 7.1% |
| Stage B `negative` | 4 | 4.8% |
| **Stage 1 admitted** | **83** | **98.8%** |
| `is_reproduction` flagged among the 74 positives | 7 | 9.5% |

**Second variant (sheet `abstract_r` substituted where OpenAlex has none):** identical
— Stage A 83, verdicts 74/6/4, admitted 83. Only one indexed work lacks an OpenAlex
abstract, and it passes on its title anyway. **There is no "OpenAlex missing abstract"
problem in this corpus**; the two variants separate cleanly and both point at the same
four rows.

### Stage 2 is where reproductions actually die

`rule_filter.classify_row` on the same 84:

| `filter_status` | n |
| --- | ---: |
| needs_review | 53 |
| replication | 24 |
| **false_positive (terminal)** | **5** |
| reproduction | 2 |

A `negative` verdict is terminal at Stage 2 (`filter_status = false_positive`), and
Stage 2 has **no concept arm**. So three reports that Stage 1 admits *only* because
OpenAlex tagged them with a replication/reproducibility concept are killed one stage
later. End-to-end survival of the indexed gold is **79/84 = 94.0%**, and of the whole
93-report corpus **79/93 = 84.9%**.

### The `is_reproduction` labelling gap

Only 7 of 74 positives are flagged `is_reproduction`, so 24 rows land as
`filter_status = replication` and 2 as `reproduction`. `is_reproduction_only` requires
that *every* matching phrase be a reproduction phrase, and these reports almost always
also say "replication of". Not fatal — Stage 3's screen re-decides `type` — but the
Stage 2 `reproduction` label should not be used as a count of the reproduction corpus.

## 4. The misses, one by one

Only **five** indexed reports fail, and they are not a vocabulary hole:

| Title | Failure | Category |
| --- | --- | --- |
| *Is Economics Research Replicable? Sixty Published Papers from Thirteen Journals Say "Usually Not"* | `exclusion:TECHNICAL_OBJECT` | exclusion misfire |
| *A Replication and Analysis of Tiebout Competition Using an Agent-Based Computational Model* | `exclusion:TECHNICAL_OBJECT` | exclusion misfire |
| *Social Capital and Value Creation: A Replication of 'The Role of Intrafirm Networks'* | `exclusion:TECHNICAL_VERB` | exclusion misfire |
| *Replication Data for: Savings revisited…* | `exclusion:data_repository_deposit` | sheet `doi_r` points at a Dataverse deposit, not the paper — correct behaviour, bad data |
| *Bias in Conditional and Unconditional Fixed Effects Logit Estimation: A Correction* | Stage A gate-fail, `no replication phrase detected` | genuine vocabulary hole |

The three exclusion misfires are the same failure: **TECHNICAL_OBJECT and TECHNICAL_VERB
fire on "replicate/replication of the code / data / model / method" — which is the
literal definition of a computational reproduction.** These exact papers (Tiebout,
Social Capital) are already named in `exclusion_narrowing_report.md` (issue #144), which
measured TECHNICAL_OBJECT at 25 gold positives killed for 31 marginal suppressions
(5.5/M) and TECHNICAL_VERB at 17 killed for 18 suppressions (3.2/M). **This work
independently confirms that finding from the reproduction side: those two patterns cost
more than they protect, and they cost it specifically on reproductions.**

The one real vocabulary hole is the comment/correction genre with no reproduction
vocabulary at all:

> *"In a recent paper published in this journal, Katz (2001) compares the bias… This note
> shows that while Katz's (2001) specification has 'wrong' fixed effects… his conclusions
> still hold if I correct his specification."*

No stem, no phrase, nothing. Reaching it requires admitting the commentary genre.

### Would the 9 unindexed reports be caught if they were indexed?

Yes — 8 `positive`, 1 `ambiguous`, all 9 admitted, evaluated on the title extracted from
`ref_r` plus the sheet's `abstract_r`. The phrases that fire are
`computational reproducibility`, `robustness reproducibility`, `reproduction of "`,
`reproducing the analyses`, `replication of`. **The I4R vocabulary is already covered by
the production phrase list.** These reports are missing because OpenAlex does not have
them, full stop.

## 5. Candidate vocabulary, two-sided

Right side measured on **2,892,614 real OpenAlex snapshot rows** (10 parquet partitions
in `corpus/`, streamed from `cache/snapshot/manifest.json` with column projection).
Baseline admission on that population is **1,564 rows/million**. "Extra admissions per
million" counts rows the candidate would newly admit that today's rule
(`concept_hit or verdict in {positive, ambiguous}`) does not. Full table:
`repro_vocab_candidates.csv`.

| Candidate | extra/M | proj. over 510M snapshot | gold recovered (of the 14 lost) |
| --- | ---: | ---: | ---: |
| `R4` "robustness (reproducibility\|replicability\|replication)" | 0.0 | 0 | 2 — **already in production** |
| `R2` title "Comment(s) on" + author-year cite + reproduction word in text | 0.3 | 176 | 2 |
| `R11` "in a recent paper published in this journal" + cite | 0.3 | 176 | 1 |
| `R7` "reproducibility of" + author-year cite | 4.5 | 2,294 | 1 |
| `R9` "in a recent paper/article/study" + cite | 14.5 | 7,410 | 1 |
| `R1` title "Comment(s) on" + cite (no reproduction word) | 15.6 | 7,940 | 2 |
| `R10` "this note/comment" + cite | 32.8 | 16,762 | 1 |
| `C3` robustness (report\|check\|analysis\|reproducibility\|replication) | 33.9 | 17,291 | 3 |
| `C9` bare "reproducibility of" | 53.2 | 27,172 | 1 |
| `C4` "re-examination of" | 57.0 | 29,113 | 0 |
| `C6` "the same/original/author-provided data\|code" | 102.0 | 52,050 | 1 |
| `C2` title "correction to/of" | 140.4 | 71,635 | 1 |
| `C1` title "Comment(s) on", unrestricted | 227.5 | 116,098 | 2 |
| `C5` title "revisit(ed\|ing)" | 330.5 | 168,677 | 1 |
| `C7`/`R5` "Institute for Replication" / I4R | 0.0 | 0 | 0 |

Readings:

* **C3 is a trap.** Its 3 recoveries all come from the narrow `R4` arm that production
  already has; the extra 33.9/M is bought by `robustness analysis` / `robustness check`,
  which are ordinary sensitivity analyses ("Robustness analysis of interdependent PV
  grids"). Use `R4`, not `C3`.
* **Bare "reproducibility of" (C9) is the measurement-reliability sense**, exactly as the
  code comments already say (sampled titles: assay reproducibility, detector
  reproducibility, radiological measurement). Requiring an author-year cite (`R7`) cuts
  it from 53.2/M to 4.5/M for the same single recovery.
* **The comment genre is real but expensive at title level.** OpenAlex `title.search:"a
  comment on"` reports 507,377 works. Unrestricted (`C1`) that is 227.5/M; requiring an
  author-year cite in the title drops it to 15.6/M; also requiring a reproduction word
  somewhere in the text (`R2`) drops it to 0.3/M while keeping both gold recoveries.
  **`R2` is the only genuinely cheap comment-genre rule.**
* **I4R boilerplate is not a snapshot signal.** "Institute for Replication"/"I4R" appears
  in the sheet's *citation strings*, never in an OpenAlex title or abstract — 0 fires in
  2.89M rows, 0 gold matched on OpenAlex text. There is also no I4R source record in
  OpenAlex (`/sources?search=I4R` returns nothing).
* **Every one of these candidates would also require widening Stage A**, since none of
  them contain a replication stem. That is a change to a token gate that runs over ~510M
  rows, so the per-million figures above understate the operational cost.

The blunt conclusion: **no vocabulary addition recovers an indexed reproduction that the
pipeline is losing today, except the single Katz-comment paper.** The two gold reports
that `R2` recovers are both OSF-only and unreachable anyway. Fixing TECHNICAL_OBJECT and
TECHNICAL_VERB recovers three; adding phrases recovers one.

## 6. The unreachable stratum

**9 of 93 reports (9.7%) are not in OpenAlex at all.** All 7 reports with no `doi_r` are
in this group, plus 2 whose `doi_r` is not an OpenAlex-resolvable identifier:

| Report | Where it lives | Why unreachable |
| --- | --- | --- |
| Lacko & Prošek (2025), comment on Combs et al. | osf.io/7jy4e | OSF file, no DOI |
| Créchet et al. (2024), replication of Forsythe | osf.io/7qv89 | OSF, no DOI |
| Hallman et al. (2024), *Robustness Reproducibility of "Improving Workplace Climate…"* | osf.io/txqjw | `doi_r` = `10.10419/295247` — an EconStor handle written as a DOI |
| Brodeur (2024), *Reproduction of "Who Chooses Commitment?"* | osf.io/8rb5k | OSF, no DOI |
| Kjelsrud et al. (2025), comment on Siddique et al. | osf.io/9erj6 | `doi_r` = `10.17605/OSF.IO/C3K6F` — an OSF DOI, not indexed by OpenAlex |
| de Oliveira & Meneghetti (2026) | — | no DOI, no URL |
| AOM proceedings item | — | `ref_r` is a bare DOI string `10.5465/AMPROC.2026.13271abstract` |
| PsyArXiv item | — | `ref_r` is a bare DOI string `10.31234/osf.io/am6rg_v1` |
| Bayle et al. (2026), *reproduction and replication of Nielsen and Rehbeck (2022)* | shs.hal.science | HAL working paper, not in OpenAlex |

All 9 carry unambiguous reproduction vocabulary and would be admitted immediately if the
records existed. **A curated-source ingestion path is the only way to reach them**, and
the pipeline already has the hook: `rule_filter.classify_row` bypasses the keyword filter
for `source in CURATED_SOURCES` with the comment *"I4R reproductions are titled 'A comment
on Smith et al. (2023)' and carry no replication vocabulary"*. What is missing is a Stage
1 harvester that pulls the OSF/I4R/HAL/EconStor reports into `candidates.csv` with that
source tag.

## 7. What to do

1. **Build a curated ingestion path for OSF/I4R/HAL/EconStor reproduction reports.**
   This is the whole 9.7% stratum and, on the wider I4R universe (not sampled here,
   but OpenAlex has no I4R source at all), plausibly much more. Highest value by far.
2. **Narrow TECHNICAL_OBJECT and TECHNICAL_VERB** (issue #144). Independently confirmed
   from the reproduction side: they kill 3 of 84 indexed gold reproductions, and they kill
   them precisely because a computational reproduction says "we replicated the model / the
   code / the data".
3. **Give Stage 2 the concept arm, or stop making `negative` terminal for concept-tagged
   rows.** Stage 1 admits on OpenAlex concepts; Stage 2 then discards those rows as
   `false_positive` with no appeal. That is the mechanism behind all 3 losses in (2).
4. **Optionally add `R2`** (title "Comment(s) on" + author-year cite + reproduction word):
   0.3 extra admissions/M, ~176 rows over the whole snapshot. Cheap, but it needs a Stage A
   widening to reach anything, and on this corpus it recovers only reports OpenAlex lacks.
5. **Do not treat Stage 2's `reproduction` status as a reproduction count** — it fires on
   2 of 84 known reproductions.

## Files

| File | Contents |
| --- | --- |
| `repro_full.json` | raw sheet export, A1:U4348 |
| `repro_openalex.json` | 93 deduped reports + their OpenAlex records |
| `repro_gated.json` | per-report Stage A / Stage B / Stage 2 results |
| `repro_vocab_candidates.csv` | 22 candidate patterns, two-sided |
| `repro_vocab_influx.json`, `repro_vocab_influx2.json` | raw corpus counts |
| `repro_vocab_examples*.json` | sampled titles each candidate newly admits |
| `repro_fetch.py`, `repro_retry.py`, `repro_gate.py`, `repro_stage2.py`, `repro_vocab.py`, `repro_vocab2.py` | the scripts |
