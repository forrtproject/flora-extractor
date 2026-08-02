# Known Limitations & Revisit Obligations

This document records deliberate design choices in the current pipeline that bound
recall or precision, together with the concrete **revisit obligation** each one carries.
The numbers below are from the production run of **2026-07**. These are not bugs to be
silently patched over — each is a place where a future pass should re-examine the data.

---

## (a) Recall is bounded by the Stage-2 phrase gate

Stage 2 only admits candidates that contain a replication phrase. Rows with **no
replication phrase at all (~2.17M)** are rejected at high confidence **without any LLM
review**. A second pass over this rejected bucket (embedding-based or LLM-based) is
**planned but not implemented**.

The cost of skipping LLM review is measurable on the rows that *did* get it. Of the
**132,197** phrase-without-citation rows sent to the LLM, **28,438 were readmitted**
(24,232 replication + 4,206 reproduction). That means **~58% of all accepted rows came
in via the LLM leg** — so rules-only decisions materially undercount true replications,
and the ~2.17M no-phrase rows almost certainly hide a substantial number of missed
studies.

**Revisit obligation:** implement and run a second pass (embedding or LLM) over the
no-phrase bucket before treating the accepted set as complete.

---

## (b) Exclusion-pattern misfires

The `TECHNICAL_OBJECT` and `TECHNICAL_VERB` patterns in
[`filter/spec/exclusion-patterns.yaml`](../filter/spec/exclusion-patterns.yaml)
match phrases such as *"replicated the analysis code of Smith (2019)"*. Some of these
are genuinely **in-scope computational reproductions**, which the pipeline treats as
in-scope. The patterns are kept because they
buy specificity: they drop the large volume of molecular-biology and pure-software
"replication" noise.

The narrow overlap is rescued rather than lost. When an exclusion pattern fires,
`filter/rule_filter.py` re-checks the text for a replication phrase with exclusions
ignored; if that phrase **and** an author-year citation are both present, the row is
kept as `needs_review` at medium confidence (`filter_evidence` records
`exclusion:<pattern>; phrase+cite present — LLM review`) instead of being rejected, and
reaches Stage 3's front-door screen. Rows where the exclusion fires without both
signals are still `false_positive` at high confidence and never reach the screen.

**Revisit obligation:** the rescue gate requires a *parseable* author-year citation, so
an in-scope reproduction that names its target in prose alone is still dropped — and
since the Stage-2 LLM escalation was retired, the rule filter is the only thing standing
between a `false_positive` verdict and the paper never being seen again. Measure
how much that costs before treating the technical-exclusion bucket as clean.

---

## (c) `filter_confidence` is currently uninformative

The `filter_confidence` field is **99.9% `high`** in the production run. It does not
currently discriminate between confident and marginal decisions and should not be relied
on for triage or downstream weighting until it is recalibrated.

---

## (d) Missing abstracts force title-only decisions

Of the **~2.32M filtered rows, ~494k lack an abstract**. For those rows the phrase and
LLM decisions were made on the **title only**, which is materially weaker signal.

`search/fetch_abstracts.py` backfills abstracts through a four-tier waterfall (OpenAlex
batch → Semantic Scholar batch → CrossRef by DOI → Scopus by DOI), but a backfilled
abstract does not by itself change a decision already written to `filtered.csv`: the
Stage-2 resume index makes `run_filter` skip those rows. `filter/reset_backfilled`
drops the rows that were decided empty-abstract and whose abstract has since arrived,
so they are screened again.

**Revisit obligation:** run backfill → `reset_backfilled --apply` → `run_filter` over
the previously title-only rows, and report how many decisions the abstracts changed.

---

---

## (e) Stage 1 cursor checkpoints do not account for what was fetched (issue #68)

`cache/openalex/` holds **45,866 cached result pages** but only **853 cursor
checkpoints**. The checkpoints account for 1.31M fetched records; the page files
hold up to ~9.2M. Sampling the pages shows plenty of 2012–2026 publication years,
and `candidates.csv` contains **1,077,237 rows for 2011–2021** with a smooth
year-on-year curve and no truncation cliff.

So the searches for 2012–2026 **did run** — their checkpoints are simply gone.
`cache/` is gitignored and prunable, and a cleared checkpoint leaves the fetched
rows in `candidates.csv` with nothing left to attribute them.

**Consequence:** the dashboard's *Yield per Search Phrase* table, and
`phrase_yield()` behind it, describe **only what the surviving checkpoints can
account for**. Treat a low coverage % or a `no checkpoint` badge as *missing
provenance*, not missing data. `candidates.csv` is the authority on what was
fetched.

What is still genuinely true from the checkpoints that do survive:

- The `replication of` job for **2011** stopped mid-pagination at 10,000 of 33,914
  and never resumed — `_get_page` raises `StopIteration` when OpenAlex returns
  `Retry-After > 600` (quota exhausted), saves the cursor, and nothing resumes it.
- `data/candidates.csv` was last written **2026-07-12** while cursors ran on
  **07-14**, so some fetched pages were never merged. `python -m
  search.run_search --harvest-only` merges cached pages without new API calls.

**Revisit obligation:** completeness for 2012–2021 cannot be established from the
cache. Re-running the year range is the only way to confirm it, and is cheap where
the request parameters match a cached page (`_get_page` keys its cache on the exact
param set, so identical phrase + year granularity replays for free; a different
year granularity does not hit the cache and re-spends quota).

---

## (f) Three Stage-1 "phrases" are not phrases (issue #68)

OpenAlex strips stopwords before matching, so a quoted phrase whose only content
word is a single term collapses to that one-word query. Verified 2026-07-22 by
reversing word order — an identical count means no phrase matching:

- `"replication of"` = `"of replication"` = `"replication"` = **1,299,397** works
- `"direct replication"` = 1,809, reversed = 115 → genuine phrase match
- `"we replicated"` = 14,023, reversed = 9,168 → genuine phrase match

The degenerate ones are `replication of`, `reproducibility of` and
`replicability of` — also the three highest-yield phrases. They are firehoses
standing in for high-precision phrases, which inflates Stage 1 volume and pushes
the precision burden entirely onto Stage 2.

**Revisit obligation:** decide whether to keep them as deliberate broad recall
(and say so in the technical report) or replace them with genuine
multi-content-word phrases. Do not extend `openalex_search._OA_STOPWORDS` on
intuition — `we`, `not`, `did` and `could` were each measured *not* to be dropped.

## (g) The cheap pre-screen trades a measured property for money (issue #130)

`shared/prescreen.py` is ON by default (decided 2026-08-03: #129 makes the snapshot scan
the only path into Stage 3, and that population cannot be screened at full price).
`screen_gate()` has a measured *zero* settled misses; a pre-screen discard is terminal —
the row never reaches the validated screen, never reaches `csv_to_db`, never reaches a
human — so nothing about the screen's property extends to the tier in front of it.

**The AND gate is weaker than it looks.** Over 567 gold positives the two voters fail
together 37.8x more often than independence predicts: when one misses a paper the other
misses the same one 80% of the time, against a 2.1% base rate. Four of five misses are
joint. The deterministic override caught all four, so the safety rests on a hand-written
regex rather than on two opinions — and that regex was written after seeing those misses.
A phrasing it does not know is the live failure mode. Dropping to one voter and keeping
the override costs exactly one paper in 567 and gains four points of discard rate, so the
second model is insurance against provider drift and against this eval's easy-positive
bias — not the safety mechanism it was presented as. A third voter would add less still.

Two limits of the evidence in `analysis/prescreen_eval/REPORT.md` are structural and no
larger run of the same design would fix them:

- **The miss rate cannot be bounded tightly enough.** The exact 95% interval on zero
  misses in 567 gold positives still reaches 0.67%. Bounding the true
  rate below 0.5% would need roughly 1,450 independent gold positives with the same two
  misses, and the FLoRA database does not contain that many that also reach Stage 3.
- **The gold positives are the easy positives.** They are canonical, well-described
  replications already in FLoRA. The marginal, oddly-phrased papers that only keyword
  search finds are exactly what a 3B-class model misses and are structurally absent
  from the set, so the measured rate is a lower bound for the stratum that matters.

**Revisit obligation:** run `PRESCREEN_MODE=shadow` over the first pass of the new
corpus — verdict recorded, discard not acted on — and count how often it would discard a
row the validated screen went on to keep. Sample `data/prescreen_discard.csv` by hand
regularly once it is discarding: nothing else ever looks at those rows again, and
agreement between two correlated models is not evidence they were right. That quantity is the real cost, it is
measurable in the thousands without any gold labels, and it is the only number that
should decide this. Re-check the economics at the same time: measured over
`data/filtered.csv` on 2026-08-02, 49,800 of 2,581,092 rows reach Stage 3, so the whole
screening bill is ~$87 and this tier nets ~$30 of it.
