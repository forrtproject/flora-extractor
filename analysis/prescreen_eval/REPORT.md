# Pre-screen evaluation — findings (issue #130)

Run 2026-08-02. Voters: `google/gemini-2.5-flash-lite` + `mistralai/mistral-nemo` via
OpenRouter. Prompt: `prompt_p4.txt`, which is `_PRESCREEN_PROMPT` in `shared/prompts.py`.

## Summary

| | measured | 95% CI |
| --- | --: | --: |
| screen-confirmed negatives discarded (n=184) | 44% | 37–51% |
| …after the hard-signal override rescues 27 of them | **29%** | 23–36% |
| gold positives lost (n=567) | **0** | 0.00–0.67% |

The tier works, at a saving far smaller than the issue assumes, and the deterministic
override — not the models — is what makes it safe.

## Three findings that change the issue's premises

**1. The economics are ~3.5× smaller than stated.** The issue budgets ~180,000 rows
through Stage 3 at ~$315. Measured over `data/filtered.csv` on 2026-08-02, 2,532,538 of
2,581,092 rows (98.1%) are `filter_status = false_positive` and never reach Stage 3 at
all. The real population is **49,800 rows**, the whole screening bill is **~$87**, and a
29% discard rate on the ~58% of it that the screen rejects saves about **$15 per full
corpus pass**. At that size the tier is not worth enabling for its own sake; it is worth
having built and measured in case #129 changes the volume by an order of magnitude.

**2. The simple-prompt hypothesis is wrong — the opposite is true.** The issue proposes
that a minimal, positively framed question would beat the production screen's enumerated
exclusions, because a long conditional list gives a small model more ways to argue
"clearly out". Measured on the same 184 screen-confirmed negatives:

| prompt | flash-lite | nemo | AND-gate |
| --- | --: | --: | --: |
| `p1` one question, positive framing (the issue's proposal) | 5% | 1% | ~0% |
| `p3` p1 plus "you are the cheap first filter, when in doubt keep" | 1%* | 0%* | ~0% |
| `p4` names what does **not** count (no enumerated edge cases) | 58% | 55% | 44% |

\* partial runs, stopped when the direction was unambiguous.

Naming what does not count is what makes a small model able to discard anything at all.
`p1` and `p3` are perfectly safe and completely useless: they keep essentially the whole
corpus, so the tier costs money and saves none. This is the single clearest result here,
and it is worth recording because the intuition behind it was reasonable.

Note that `p4` carries no enumerated edge-case list — no biological, engineering or
distributed-systems clauses. It states the construct positively, then names three ways
of not qualifying in general terms. That appears to be the useful middle: enough
structure to license a "no", no catalogue of special cases.

**3. The Suiter miss was a prompt artifact, not a model limit.** The issue's sharpest
failure — an abstract opening "The purpose of this prospective, double-blinded,
multirater, systematic replication study was to…" discarded by both cheap models — was
kept by every model and every prompt tested here. It is also caught by the deterministic
override before any model is asked, so it cannot recur.

## The override is what carries the safety

Five gold positives were discarded by the binding voter. **All five carry an explicit
replication phrase that `hard_signal()` matches:**

| case | DOI | phrase |
| --- | --- | --- |
| GP201 | 10.1027/1614-0001/a000082 | "We replicated" |
| GP395 | 10.1089/cap.2024.0078 | "Replication of the" |
| GP565 | 10.1177/0734282915580885 | "Replication of the" |
| GP646 | 10.1186/s12888-023-04903-9 | "replication of the" |
| GR021 | 10.1017/psrm.2017.44 | "we replicate" |

So the measured gold-positive loss with the override on is **0 of 567**. The override
also fires on 31% of the confirmed negatives — that is its cost, and it is the right
cost to pay: a needless override sends one row to a $0.0018 screen call, while a missed
one loses a replication study permanently. The gap between the 44% raw and 29% net
discard rate is exactly this.

Stage 2's own verdict cannot serve as the override. 98% of rows that reach Stage 3 carry
`filter_status = replication` at `high` confidence — including **all 184** screen-confirmed
negatives — so bypassing on it would disable the tier entirely.

## What this evaluation cannot tell you

**The positive-side result is exact; the raw AND-gate figure behind it is a bound.**
OpenRouter credits ran out (HTTP 402) partway through flash-lite's positive buckets, and
`score_prescreen.py` prints an `INCOMPLETE — AND is a floor` warning for them. Read the
0-loss figure this way: the override runs **before** any model is asked, so a row it
rescues is kept whatever the voters would have said, and all five candidate losses are
rescued. That holds at any coverage. What is *not* measured is how many of the five the
AND-gate would have discarded on its own — flash-lite never scored any of them — so the
override's contribution is bounded above by 5 of 567 rather than pinned. What is also
missing is the second benefit estimate on the curated-negative bucket.

**`inclusionai/ling-2.6-flash` could not be evaluated.** It returned HTTP 429 for every
call through OpenRouter on 2026-08-02, at every concurrency from 1 to 10 and with and
without cheapest-provider pinning. Whatever it scores, it cannot gate a corpus, so
`PRESCREEN_VOTER1_MODEL` defaults to `google/gemini-2.5-flash-lite` instead.

**Zero observed misses is not a bounded miss rate.** The 95% interval on 0/567 still
reaches 0.67%. Bounding the true rate below 0.5% needs ~600 gold positives with zero
misses — roughly what is here — but the interval is one-sided comfort only if the sample
is representative, and it is not:

- **The gold positives are the easy positives.** They are canonical, well-described
  replications already in FLoRA. The marginal, oddly-phrased papers that only keyword
  search finds are what a small model misses, and they are structurally absent.
- **The negatives are labelled by the system under test.** The 184 "screen-confirmed"
  negatives carry the validated screen's own error rate, so the 29% is agreement with
  the big screen, not accuracy against truth.
- **These rows are derivation data.** They are the same population that motivated the
  issue, and `p4` was chosen on them. The effect size (44% vs ~0%) is far too large for
  a winner's curse to explain, but the exact rates are in-sample.

## Recommendation

Ship the tier, leave `PRESCREEN_ENABLED` off, and do not turn it on for $15.

Before enabling it, run it in **shadow**: record `prescreen_verdict` on every row, act on
no discard, and count how often it would have discarded a row the validated screen then
kept. That is the quantity that actually matters — an incremental loss, not a miss
against a gold list — it needs no gold labels, it is measurable in the thousands on the
live population, and it is the only number that should decide this. Revisit when #129
settles the corpus size.
