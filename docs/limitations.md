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
hard-exclude phrases such as *"replicated the analysis code of Smith (2019)"*. Some of
these are genuinely **in-scope computational reproductions** — and the Stage-2 LLM
prompt itself treats same-data reproduction as in-scope. So the exclusion regex and the
LLM prompt currently disagree on this class.

This is **deliberate for now**: the patterns buy specificity (they correctly drop the
large volume of molecular-biology and pure-software "replication" noise), and losing a
few reproductions is the accepted cost.

**Revisit obligation:** re-examine rows where a `TECHNICAL_*` exclusion fired **and** a
replication phrase **and** an author-year citation were also present, and readmit the
computational-reproduction misfires.

---

## (c) `filter_confidence` is currently uninformative

The `filter_confidence` field is **99.9% `high`** in the production run. It does not
currently discriminate between confident and marginal decisions and should not be relied
on for triage or downstream weighting until it is recalibrated.

---

## (d) Missing abstracts force title-only decisions

Of the **~2.32M filtered rows, ~494k lack an abstract**. For those rows the phrase and
LLM decisions were made on the **title only**, which is materially weaker signal. An
abstract-backfill fix is **in progress** (including a planned Scopus tier in the backfill
waterfall — see the in-flight-changes note in `CLAUDE.md`).

**Revisit obligation:** once abstract backfill lands, re-run Stage 2 over the
previously title-only rows.

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
