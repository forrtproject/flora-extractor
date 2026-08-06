# Known Limitations & Revisit Obligations

This document records deliberate design choices in the current pipeline that bound
recall or precision, together with the concrete **revisit obligation** each one carries.
These are not bugs to be silently patched over — each is a place where a future pass
should re-examine the data.

Every measurement here is date-stamped and names the run or report it came from,
**and the segment of the corpus it was measured over**. A rate quoted over an
unsegmented corpus is not a rate: a 5.4% PDF-escalation figure computed over a
mixed file was 92% once legacy rows were excluded and only fresh rows counted, and
nothing in the number itself said which population it described. Name the
denominator or do not quote the figure.

Where an entry is marked **historical** or **superseded**, the number was measured on
the retired `filtered.csv` of the **2026-07** production run and does not describe the
current pipeline. Last reviewed against the code on **2026-08-04**.

---

## (a) Recall is bounded by the search gate

**Stage 1 searches; Stage 2 filters.** The snapshot scanner
(`search/snapshot_scan.py`) applies the **search gate** and nothing else: a broad
token/stem alternation over title and raw abstract, **or** membership of a
replication concept. Either hit puts the work in the survivor pool. No exclusion
pattern and no phrase precision test runs in Stage 1, so a rule change is a
`filter.engine route` re-run over the pool rather than a rescan, and the spec
bundle is the one rule set that decides what a paper is.

The bound that remains is the gate itself. A work whose title and abstract carry
none of the stems and which OpenAlex tagged with neither concept never enters the
pool, and nothing downstream can recover it: Stage 2 routes and discards over the
pool, **only LLM tiers admit** there, and a rule can never turn a discard back into
a keep. The gate is a token vocabulary plus two concept ids, applied once, at scan
time, with no LLM review of what it rejected — and a full rescan is the only way
back. That asymmetry is why a token added to the gate is the expensive kind of
change (it also enlarges the artifact every collaborator downloads) while a spec
edit is the cheap kind.

The historical measurement behind this entry, from the retired `filtered.csv`
(production run of **2026-07**): rows with no replication phrase at all (~2.17M) were
rejected at high confidence without any LLM review, while of the 132,197
phrase-without-citation rows that *were* sent to the LLM, 28,438 were readmitted
(24,232 replication + 4,206 reproduction) — ~58% of all accepted rows arrived via the
LLM leg. Those numbers describe a pipeline that no longer exists; they are kept
because the shape of the finding — rules-only admission materially undercounts — is
what motivates the obligation below.

**Revisit obligation:** implement and run a second pass (embedding or LLM) over the
bucket the search gate rejects, and re-measure the readmission rate on the current
architecture before treating the accepted set as complete.

---

## (b) Exclusion-pattern misfires

The `TECHNICAL_OBJECT` and `TECHNICAL_VERB` patterns — now the specs
[`technical-object`](../filter/spec/technical-object.json) and
[`technical-verb`](../filter/spec/technical-verb.json) —
match phrases such as *"replicated the analysis code of Smith (2019)"*. Some of these
are genuinely **in-scope computational reproductions**, which the pipeline treats as
in-scope. The patterns are kept because they
buy specificity: they drop the large volume of molecular-biology and pure-software
"replication" noise.

The narrow overlap is rescued rather than lost. The `exclusion-rescue` spec
outranks the exclusion band: when an exclusion pattern fires but the text also
carries a replication phrase **and** an author-year citation, the row routes to
`screen_cheap` instead of being discarded, and so still reaches a screening tier.
Rows where the exclusion fires without both signals are discarded at high
confidence and never reach a screen.

**Revisit obligation:** the rescue requires a *parseable* author-year citation, so
an in-scope reproduction that names its target in prose alone is still dropped —
and a discard is the one rule-terminal state, with nothing standing between it and
the paper never being seen again. Measure how much that costs before treating the
technical-exclusion bucket as clean.

---

## (c) `filter_confidence` does not discriminate

**Superseded measurement.** The "99.9% `high`" figure was measured on the retired
`filtered.csv` of the **2026-07** production run, produced by a filter stage that no
longer exists. Do not quote it.

The field is uninformative for a different reason now: under the filter engine,
`filter_confidence` is a **constant per pile**, read from
`filter/spec/conventions.json` (`screen_expensive` → high, `screen_cheap` → medium,
`needs_human` → low, `discard` → high). It labels which pile a row came from, not how
sure anything was about that row, and two rows in the same pile always carry the same
value. It should not be used for triage or downstream weighting.

---

## (d) Missing abstracts force title-only decisions

Of the **~2.32M rows the retired Stage 2 filtered, ~494k lacked an abstract**. For
those rows the phrase and LLM decisions were made on the **title only**, which is
materially weaker signal, and those decisions are what the corpus on disk carries.

The filter engine no longer decides such a row at all: a work routed to a screening
pile with no abstract is downgraded to `pending/no_text`, because absence of
evidence must not convert into a proceed. Text arrives through a **text overlay** —
`python -m filter.engine worklist` exports the `no_text` rows,
`python -m filter.engine.backfill` fetches them through Stage 1's six sources — a
cheap bulk pathway over everything, then a gated one over what is still missing — and
a frozen overlay folds into the release id, so re-routing under it genuinely
re-decides those rows.

**Revisit obligation:** the ~494k figure above is the old `filtered.csv`. Re-measure
it as a `pending/no_text` count on the current release, run the worklist → backfill
→ freeze → `route` cycle over it, and report how many piles the recovered abstracts
changed.

---

## (e) Stage 1 cursor checkpoints do not account for what was fetched (issue #68) — **superseded**

**Superseded 2026-08-03.** This entry describes the API-harvest Stage 1, whose
sources are retired to `wip/api-harvest-sources` (PR #158) and whose corpus
(`data/candidates.csv`) nothing downstream reads. The revisit obligation was
discharged by a different route than the one below: the full OpenAlex snapshot was
scanned once — all 510,372,821 records, no cursors and no per-phrase quota — into
the survivor pool, so completeness for 2012–2021 no longer rests on what a cache of
result pages can account for. The account below is kept as the record of why the
old numbers cannot be reconstructed.

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

`shared/prescreen.py` is off by default and should stay off until someone decides the
trade deliberately. `screen_gate()` has a measured *zero* settled misses; a pre-screen
discard is terminal — the row never reaches the validated screen, never reaches
validation, never reaches a human — so nothing about the screen's property extends to
the tier in front of it.

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

**Revisit obligation:** before enabling this in production, run it in shadow — verdict
recorded, discard not acted on — over fresh rows and count how often it would discard a
row the validated screen went on to keep. That quantity is the real cost, it is
measurable in the thousands without any gold labels, and it is the only number that
should decide this. Re-check the economics at the same time: measured over
`data/filtered.csv` on 2026-08-02, 49,800 of 2,581,092 rows reach Stage 3, so the whole
screening bill is ~$87 and this tier nets ~$30 of it.

---

## (h) Full-text acquisition fails on most rows that reach it

**Historical measurement, 2026-07 production corpus.** Of the rows that escalated to
PDF acquisition, the waterfall in `shared/pdf_sources.py` returned no usable
document **62%** of the time. That bounds every step behind it: the full-text link
rung, the full-text outcome pass, and `record_type_check` are all unavailable on
those rows, which is a large part of why `cannot_be_determined` and `pending` are
common outcomes rather than rare ones.

The denominator matters here more than the rate. It is *rows that reached
acquisition*, not rows in the corpus, and the population that reaches acquisition is
the one the cheaper rungs could not settle — the hard tail, and the tail most likely
to be paywalled. Do not restate it as a corpus-wide open-access rate.

**Revisit obligation:** re-measure on the current corpus, segmented by
`pdf_source`/`parse_method` (both are written on every row precisely so this is
answerable from the CSV), and decide from the failure mix whether another
acquisition tier would pay for itself or whether the misses are structurally
paywalled.
