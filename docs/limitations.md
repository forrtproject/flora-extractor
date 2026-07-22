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

## (e) Stage 1 OpenAlex coverage is far from complete (issue #68)

Two independent problems, both measured against the live API on 2026-07-22.

**1. The year loop never advanced past 2011.** `run_search`'s job defaults are
`from_year=2011, to_year=2021`, but the cursor checkpoints on disk only cover
**1990–2011**, and 2011 itself stopped mid-pagination. For the largest phrase:

| Years | OpenAlex reports | Fetched |
| ----- | ---------------- | ------- |
| 1990–2010 | 315,057 | 315,344 (complete) |
| 2011 | 33,914 | 10,000 (cut off) |
| 2012–2026 | 950,426 | **0 — never run** |
| **total** | **1,299,397** | **325,344 (25%)** |

Post-2011 is where publication volume is highest, so the missing years are the
majority of the corpus. `_get_page` raises `StopIteration` when OpenAlex returns
`Retry-After > 600` (daily quota exhausted) and the phrase stops with its cursor
saved — correct behaviour, but nothing tracked that the run was never resumed.

**Recovery:** re-run Stage 1 for the missing span. Cursors resume in place, so
already-fetched pages cost nothing:

```bash
python -m search.run_search --auto-advance --from-year 2011 --to-year 2026
```

**Prevention:** cursor checkpoints now record OpenAlex's own `meta.count` as
`api_total`, and the dashboard's *Yield per Search Phrase* table shows
fetched-vs-expected coverage, a *cut off* badge for jobs that stopped
mid-pagination, and a *never run* badge listing years with no job at all. Jobs
written before this change have no `api_total`, so their expected column stays
blank until the phrase is re-run — `expected_partial` flags that.

**2. Three "phrases" are not phrases.** OpenAlex strips stopwords before matching,
so a quoted phrase whose only content word is a single term collapses to that
one-word query. Verified by reversing the word order (identical count ⇒ no phrase
match): `"replication of"` = `"of replication"` = `"replication"` = 1,299,397 works,
whereas `"direct replication"` (1,809) ≠ reversed (115).

The affected phrases are `replication of`, `reproducibility of`, and
`replicability of` — which are also the three highest-yield phrases (377k + 325k +
315k = 78% of everything fetched). They are firehoses standing in for
high-precision phrases, which inflates Stage 1 volume and pushes the precision
burden entirely onto Stage 2. The dashboard flags them **not a phrase**.

**Revisit obligation:** decide whether to keep them as deliberate broad recall
(and say so in the technical report) or replace them with genuine multi-content-word
phrases. Do not extend the stopword set in `openalex_search._OA_STOPWORDS` on
intuition — `we`, `not`, `did` and `could` were each measured *not* to be dropped.
