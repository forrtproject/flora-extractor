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
