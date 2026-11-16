# Screen voter combinations (2026-11-04)

This report compares every screening-model combination we have tested for PR #219 and issue #227.
All of them were scored on the same 2,186 labelled papers. The scoring used the per-paper answers
from the October runs, plus seven decision models run on 2026-11-03. Those seven runs had no
errors and cost $1.46. Span-01 and Span-01 Lite followed on 2026-11-04, also without errors, for
$0.10. They accept only a yes/no question, so `run_span01.py` asks the production rules as
"qualifies?". Their P(yes) is the same quantity the gate thresholds, but they give no type.

Scripts: `combos.py` (scores any combination: discard, type, and precision under the strict gate),
`stage2_upstream.py` (the cases Stage 2 discards before the screen), `score_new.py` and `served.py`
(the full sweeps, → `*_out.txt`), `rescreen_projection.py` (issue #227's cost and rescue
projection, → `rescreen_out.txt`), and `run_new.py` / `run_span01.py` (the 2026-11-03 and 11-04
runs). The answers are in `out/`; the older ones are in `../jev_eval_2026-10/out/`. The
validators' type truth is snapshotted in `type_truth.json` (642 positives, read from Supabase
2026-11-04). The scorers run offline with `python <script>` from this folder; only the two `run_*`
scripts call the API.

## How to read the table

- **Rule.** A paper is discarded only when every voter says it is not a replication.
  `Span-02 < 0.7` means Span-02 counts as saying "not a replication" when its probability is below 0.7.
- **Lost.** Real replications the screen discards, out of 1,456. They never reach Stage 3. Lower is better.
- **Human / pipeline.** The share of non-replications the screen discards. Higher is better.
  - **Human** covers 161 papers a person judged not to be replications. This is the reliable figure.
  - **Pipeline** covers 569 papers labelled by an earlier screen or an older LLM, which nobody checked.
- **$/1k.** The cost of screening 1,000 papers with every voter in the combination.
- **Rescued / new.** The comparison with main (DeepSeek + Luna). *Rescued* is how many of main's 8
  lost replications this combination keeps. *New* is how many it loses that main keeps.
  Lost = 8 − rescued + new.
- **Type correct.** Of the positives that validators typed, the share where the screen's
  replication-vs-reproduction label matches theirs. The share is of the positives the screen gave
  any type, roughly 630–640 of 642.
- **Repro /43.** How many of the 43 validator-confirmed reproductions the screen labels as a
  reproduction. A missed one is coded as a replication, so its outcome is coded on the wrong scale.
- **How a combination picks the type.** This is the production rule. The text-answering voter's
  type stands: Luna's, or DeepSeek's when DeepSeek is listed first. A decision model overrides it
  only when it names a different type with at least 70% of its "qualifies" probability.
- **Prompt caching.** This column names which voters in the combination get a cache discount.
  The screening rules are the same long block on every paper, so a cached prefix makes the next
  paper's input cheap. Measured in the October runs:
  - **Luna:** explicit cache breakpoints sent by `shared/llm_client.py`. 88% of input tokens
    were served from cache, at $0.01 instead of $0.10 per M tokens.
  - **DeepSeek V4 / V4.1 Flash:** automatic on OpenRouter. 77% / 88% cached, at $0.018 instead
    of $0.091 / $0.045 per M tokens.
  - **Decision models** (Span-02, d1.1, Clef, Perplexity, Mammoth…): **no caching discount.**
    d1.1 and Drex Max list a cache price equal to the full price; the others list none. They bill
    input only, with no output tokens, so each call's cost is fixed and predictable.

  Caching does not help a voter that is not called, and it never changes an answer.
- **Noise.** Every loss comes from the 17 replications Luna misses, and the cut-offs were chosen on
  this same data. A difference of 1–3 lost, or of 2–3 reproductions, is within chance.

## Combinations to consider

These are the rows that matter for the decision. Main is kept as the baseline, and every one of
these combinations still has a model served today. The full list follows in the next section.

| combination | lost | human / pipeline | $/1k | rescued / new | type correct | repro /43 | prompt caching | remark |
|---|--:|--:|--:|--:|--:|--:|---|---|
| **DS4 + Luna** | 8 | 68% / 85% | 0.161 | — | 97.6% | 39 | Luna + DS4 (all voters) | Main today; the baseline every row is measured against. |
| **Luna + Span-02 < 0.7** | 5 | 68% / 81% | **0.092** | 5 / 2 | 97.8% | 39 | Luna only | **Recommended: #219 as it stands.** Cheapest, and the result does not move with the cut-off. Open weights (Apache-2.0). Loses 2 replications main keeps. |
| Luna + Liquid d1.1 < 0.5 | 3 | 63% / 80% | 0.112 | **5 / 0** | 98.0% | 36 | Luna only | Pick this if losing nothing main keeps comes first. Costs 8 more non-replications per 161 sent on to Stage 3, and finds 3 fewer reproductions. Closed weights, one host, cut-off fragile (0.55 → 6 lost). |
| Luna + Clef < 0.5 | 3 | 68% / 82% | 0.191 | 5 / 0 | 98.3% | 39 | Luna only | Strong on every column, with open weights and 3 hosts. About twice Span-02's cost. Cut-off fragile (0.6 → 8 lost). |
| Luna + Span-02 < 0.7 + Clef < 0.5 | **2** | 66% / 78% | 0.213 | **6 / 0** | **98.4%** | **41** | Luna only | Best overall, using open-weight models only: matches the withdrawn Perplexity v1.1. Needs the gate code changed to take three votes. |
| Luna + Span-02 < 0.7 + d1.1 < 0.5 | **2** | 62% / 76% | 0.134 | **6 / 0** | 98.1% | 38 | Luna only | Cheapest way to reach 2 lost, but discards the fewest non-replications. Also needs three-vote gate code. |

## Precision: what each screen lets through (added 2026-11-06)

Lukas's review of #219 pointed out a gap: the tables above rank screens by replications lost,
but every non-replication a screen passes costs validator time. This section measures that side.

### What was tried, and what did not work

The aim was a change inside #219's two-vote gate that discards more non-replications without
losing more replications. The gate sees only what each stored vote keeps: Luna's class and
confidence, and Span-02's vote, which is "none" below 0.7 and "confident none" at 0.1 or less. Each
candidate rule was chosen on one half of the cases (split by a hash of the case id) and checked on
the other half.

| family of rules | examples | result on the held-out half |
|---|---|---|
| Discard where Luna is unsure | Luna "unclear", or qualifying but unconfident, while Span-02 says none / confident none | Luna is unsure on only 50 of 2,186 cases. Each extra discard costs about one real replication per 1–2 non-replications. |
| Let Span-02 veto | discard whenever Span-02 is below 0.01–0.2, whatever Luna says | 6 to 38 lost, against 5 now |
| Raise the cut-off | 0.8, 0.9 | +1 / +3 lost for +2 / +4 human and +10 / +16 pipeline negatives discarded |
| Raise it only when Span-02's doubt is "none" rather than "unclear" | below 0.8–0.99, and only when P(none) ≥ 0.5–5 × P(unclear) | no better than a plain cut-off; every variant loses 1 more replication on the held-out half |

**None of these is an improvement, so the gate in #219 is left unchanged.** Every stricter rule
trades about one real replication for 2 human-labelled and 10 pipeline-labelled non-replications.
That is the same ~12:1 exchange rate that argued against a third voter, in the other direction.

The reason shows in the 52 human-judged non-replications the pair lets through. 32 of them are
papers both models confidently call replications, with Span-02 at 0.9 or higher. Most are
registrations, protocols, review records and replication-adjacent discussion pieces. No
combination of these two votes can tell them apart from real replications; only reading more
than the abstract can.

### What did improve: measuring the population the screen actually sees, and the strict gate

Two corrections make the precision figures match production.

1. **Stage 2 discards some of these papers before the screen runs.** `stage2_upstream.py` applies
   the live record-about-a-paper discard rules from main: `not-a-paper-doi`, `-title` and
   `-abstract`, and `peer-review-record`. The record types come from OpenAlex
   (`openalex_types.json`). They remove 10 cases: 7 human-labelled negatives (PCI decisions and
   reviews, a peer review, a Faculty Opinions recommendation), 1 pipeline negative, and 2 cases
   labelled positive that are themselves PCI or review records, so their labels are wrong. The
   screen is then scored on the remaining 2,176.
2. **#229's strict gate.** For works routed by a `strict` rule, a pass is admitted only when every
   vote is qualifying and confident. Otherwise it is deferred: held back, never discarded, and not
   sent to validators. `precision()` in `combos.py` applies the same test to the stored votes.

Columns:

- **Non-replications passed:** human-labelled out of 154, and pipeline-labelled out of 568. Lower
  is better.
- **Per 100 kept:** non-replications passed for every 100 real replications kept.
- **Strict gate:** what is admitted when the routing rule is `strict`. Deferred replications are
  not lost; they wait for the gate to be relaxed.

| combination | lost | non-replications passed, human / pipeline (screen alone → after Stage 2) | per 100 kept | strict: replications deferred | strict: non-replications admitted, human / pipeline | strict: per 100 admitted |
|---|--:|--:|--:|--:|--:|--:|
| **DS4 + Luna (main)** | 8 | 51 / 88 → **45 / 88** | 9.2 | 85 | 22 / 34 | 4.1 |
| **Luna + Span-02 < 0.7 (#219)** | 5 | 52 / 109 → **46 / 109** | 10.7 | 167 | **12 / 10** | **1.7** |
| Luna + Liquid d1.1 < 0.5 | 3 | 59 / 115 → 54 / 115 | 11.6 | 273 | 6 / 1 | 0.6 |
| Luna + Clef < 0.5 | 3 | 52 / 103 → 46 / 103 | 10.3 | 274 | 8 / 1 | 0.8 |
| Luna + Span-02 + Clef | 2 | 54 / 123 → 48 / 123 | 11.8 | 308 | 8 / 1 | 0.8 |
| Luna + Span-02 + d1.1 | 2 | 61 / 135 → 55 / 135 | 13.1 | 303 | 6 / 1 | 0.6 |
| *Luna + Span-02 < 0.8 (dial)* | 6 | 50 / 99 → 44 / 99 | 9.9 | 166 | 12 / 10 | 1.7 |
| *Luna + Span-02 < 0.9 (dial)* | 8 | 48 / 93 → 43 / 93 | 9.4 | 164 | 12 / 10 | 1.7 |

What this changes:

- **Under lenient rules, #219 lets through 22 more non-replications than main** (1 human, 21
  pipeline) for 3 fewer replications lost: about 7 non-replications per replication saved. The
  third voter's trade was 12. Most of the extra passes are pipeline-labelled, and part of that
  label set came from earlier DeepSeek screens, so DeepSeek's agreement with it is partly
  circular.
- **Under strict rules, #219 admits less than half main's non-replications** (22 against 56). It
  pays for that by deferring 167 replications against 85; deferred works wait for the gate to be
  relaxed, at no new screening cost. Which effect dominates in production depends on how many
  works are routed by strict rules. #229 measured 14% of passes deferred under main's pair; Lukas
  estimates about 28% under Span-02.
- **The three-voter rows are worst on lenient precision** (11.8 and 13.1 per 100), which confirms
  Lukas's point. Under strict rules their extra passes are almost all deferred, so the third
  voter's recall gain would mostly sit in the deferred pile.
- **The cut-off dial is available if validator load matters more.** Raising Span-02's cut-off to
  0.8 passes 12 fewer non-replications (2 human, 10 pipeline) for 1 more replication lost. It is
  one constant, `DECISION_DISCARD_BELOW`, and the cached probabilities make the change free to
  apply. At 12:1 it is break-even on Lukas's own exchange rate, so this report does not
  recommend it.

**Recommendation: merge #219 as it is, with two voters at 0.7.** No rule over two votes improves
precision without losing replications at the same rate. The non-replications that still get
through are best caught downstream: Stage 3's `record_type_check` and prospective-registration
quarantine, and Stage 2 record-type rules like #223.

## All combinations

DS4 is DeepSeek V4 Flash and DS4.1 is DeepSeek V4.1 Flash. Luna and DeepSeek answer in text, and
that text gives each row its evidence quote and reasoning. Every other voter returns probabilities
only.

| combination | lost | human / pipeline | $/1k | rescued / new | type correct | repro /43 | prompt caching | remark |
|---|--:|--:|--:|--:|--:|--:|---|---|
| *Luna alone (reference)* | 17 | 73% / 86% | 0.070 | 0 / 9 | 97.0% | 33 | Luna (all voters) | Not a proposed gate; shows what the second voter adds. |
| **DS4 + Luna** | 8 | 68% / 85% | 0.161 | — | 97.6% | 39 | Luna + DS4 (all voters) | Main today; the baseline. |
| **Luna + Perplexity v1.1 < 0.4** | **2** | 67% / 82% | 0.113 | **6 / 0** | 98.1% | 38 | Luna only | Best pair ever measured, and #219's first proposal. **Withdrawn** (404). |
| Luna + Perplexity v1.2 < 0.4 | 5 | 65% / 80% | 0.113 | 4 / 1 | 97.8% | 34 | Luna only | v1.1's successor; worse on every column. Perplexity withdraws versions within days. |
| Luna + Perplexity Flash v1.2 < 0.4 | 5 | 63% / 77% | 0.091 | 4 / 1 | 97.5% | 37 | Luna only | Cheap, but discards less. |
| **Luna + Span-02 < 0.7** | 5 | 68% / 81% | **0.092** | 5 / 2 | 97.8% | 39 | Luna only | **#219 now.** Cheapest. The result holds at any cut-off from 0.2 to 0.7. Open weights (Apache-2.0). Loses 2 replications main keeps. |
| Luna + Span-01 < 0.3 | 5 | 63% / 79% | 0.114 | 5 / 2 | 97.0% | 33 | Luna only | Span-02's predecessor. Matches its losses only at a low cut-off, which discards 5 points less. Costs twice Span-02's price ($0.044 vs $0.022 per 1k on its own). Yes/no only, so it gives no type: 6 fewer reproductions found. |
| Luna + Span-01 < 0.7 | 7 | 69% / 83% | 0.114 | 4 / 3 | 97.0% | 33 | Luna only | At Span-02's cut-off it loses 2 more. Worse than Span-02 on every column except discard. |
| Luna + Span-01 Lite < 0.3 | 5 | 64% / 78% | **0.070** | 5 / 2 | 97.0% | 33 | Luna only | Free tier, with answers matching Span-01's on half the papers (median difference 0). Costs only Luna, but free endpoints are rate-limited and carry no service promise, so it suits a trial, not production. Same no-type drawback. |
| Luna + Span-01 Lite < 0.7 | 7 | 68% / 83% | 0.070 | 4 / 3 | 97.0% | 33 | Luna only | Like Span-01 at 0.7. |
| **Luna + Liquid d1.1 < 0.5** | 3 | 63% / 80% | 0.112 | **5 / 0** | 98.0% | 36 | Luna only | Never loses a replication main keeps, but finds 3 fewer reproductions than Span-02. Discards 8 fewer human-labelled negatives. Closed weights, one host, cut-off fragile (0.55 → 6 lost). |
| Luna + Liquid d1.1 < 0.4 | 2 | 58% / 77% | 0.112 | 6 / 0 | 98.0% | 36 | Luna only | Matches v1.1's recall, but sends about 15 more non-replications on to Stage 3. |
| Luna + Clef < 0.5 | 3 | 68% / 82% | 0.191 | 5 / 0 | 98.3% | 39 | Luna only | Good on every column. Open weights and 3 hosts. Cut-off fragile: at 0.6 it loses 8 (3 rescued / 3 new). |
| Luna + Mammoth-1 < 0.4 | 2 | 63% / 75% | 0.285 | 6 / 0 | 97.8% | **42** | Luna only | Matches v1.1's recall and finds the most reproductions of any combination. Open weights (MIT). 2.5× d1.1's price and discards less. |
| Luna + Mammoth-1 < 0.5 | 4 | 67% / 79% | 0.285 | 5 / 1 | 97.8% | 42 | Luna only | Same type strength; costly. |
| Luna + Elephant-1 < 0.4 | 3 | 62% / 81% | 0.145 | 5 / 0 | 97.7% | 36 | Luna only | Open weights (MIT). Below d1.1 at a higher price. |
| Luna + Clef Flash < 0.6 | 4 | 63% / 76% | 0.118 | 4 / 0 | 97.8% | 39 | Luna only | Below Clef on every column. |
| Luna + Drex v1.5 < 0.7 | 4 | 63% / 80% | 0.153 | 4 / 0 | 97.3% | 32 | Luna only | Weak on reproductions. |
| Luna + Drex v2.0 < 0.3 | 3 | 57% / 72% | 0.195 | 5 / 0 | 98.0% | 36 | Luna only | Reaches 3 lost only by discarding few negatives. |
| Luna + Tev1 < 0.6 | 4 | 61% / 78% | 0.162 | 4 / 0 | 96.9% | 26 | Luna only | Experimental model; adds nothing on type. |
| Luna + Jev 1.14 < 0.3 | 4 | 65% / 81% | 0.172 | 4 / 0 | 97.6% | 38 | Luna only | Worse at any higher cut-off. |
| Luna + Kev < 0.5 | 5 | 60% / 72% | 0.157 | 4 / 1 | 97.3% | 32 | Luna only | Discards least. |
| Luna + MS-Decision-1 < 0.3 | 10 | 70% / 85% | 0.158 | 2 / 4 | 97.1% | 33 | Luna only | Loses more than main. |
| Luna + Joey-1 < 0.5 | 13 | 69% / 80% | 0.091 | 1 / 6 | 96.4% | 24 | Luna only | 2B model; barely separates replications from the rest, and worsens types. |
| DS4.1 + Luna | 10 | 70% / 78% | 0.529 | 3 / 5 | 96.4% | 24 | Luna + DS4.1 (all voters) | Worse than V4 Flash on every column, at 3.3× the cost. |
| DS4 + Span-02 < 0.7 | 7 | 73% / 84% | 0.113 | 5 / 4 | 97.8% | 40 | DS4 only | Dropping Luna buys discard but loses 4 replications main keeps. |
| DS4 + d1.1 < 0.5 | 4 | 64% / 83% | 0.133 | 5 / 1 | 98.1% | 37 | DS4 only | Below Luna + d1.1 on loss. |
| DS4.1 + Span-02 < 0.7 | 3 | 71% / 76% | 0.481 | 6 / 1 | 97.7% | 39 | DS4.1 only | Best human-negative discard of any 3-lost combination, at 5× #219's cost. |
| Span-02 < 0.7 + Clef < 0.5 | 9 | 76% / 85% | 0.143 | 6 / 7 | 98.1% | 40 | neither (decision models only) | No LLM, so no evidence text, and 7 new losses. |
| Luna + Span-02 < 0.7 + Span-01 Lite < 0.3 | 4 | 63% / 77% | 0.092 | 6 / 2 | 97.8% | 39 | Luna only | A free third voter rescues one more, but discards 5 points less. Span-01 and Span-02 mostly agree, so stacking them adds little. |
| Luna + Span-02 < 0.7 + Clef < 0.5 | 2 | 66% / 78% | 0.213 | 6 / 0 | 98.4% | 41 | Luna only | Matches v1.1 using open-weight models only, with better type than v1.1. A third voter needs gate code changes. |
| Luna + Span-02 < 0.7 + d1.1 < 0.5 | 2 | 62% / 76% | 0.134 | 6 / 0 | 98.1% | 38 | Luna only | Cheapest way to reach 2 lost. A third voter needs gate code changes. |
| Luna + d1.1 < 0.5 + Clef < 0.5 | 2 | 61% / 77% | 0.233 | 6 / 0 | **98.6%** | 39 | Luna only | Best type accuracy, but the least discard of the triples. |
| Luna + DS4 + Span-02 < 0.7 | 3 | 66% / 79% | 0.183 | 5 / 0 | 97.7% | 39 | Luna + DS4 only | Two text voters; no gain over Luna + d1.1. |
| Luna + DS4.1 + Span-02 < 0.7 | 3 | 66% / 73% | 0.551 | 6 / 1 | 97.7% | 39 | Luna + DS4.1 only | Expensive, with no gain. |
| Luna + Span-02 < 0.7 + Clef Flash < 0.6 | 3 | 62% / 75% | 0.140 | 5 / 0 | 98.0% | 40 | Luna only | Below the Clef triple. |
| Luna + Span-02 + Clef, 2 of 3 to discard | 13 | 78% / 91% | 0.213 | — | — | — | Luna only | Majority rule discards too many replications. Every 2-of-3 gate loses 10 or more. |

## Conclusions

- **Nothing still served matches Perplexity v1.1 + Luna as a pair.** We moved off it because it
  was withdrawn, not because anything measured better.
- **#219's Luna + Span-02 is the cheapest real improvement on main.** It types as well as main and
  finds as many reproductions, and its open weights protect against a model being withdrawn. Its
  weakness is the 2 replications it loses that main keeps.
- **Luna + d1.1 < 0.5 is the alternative if losing nothing main keeps matters most.** In exchange
  it sends about 8 more non-replications per 161 on to Stage 3, finds 3 fewer reproductions, and
  carries the same single-host, closed-weight risk that ended Perplexity.
- **A third voter recovers v1.1's recall** at under $0.25 per 1,000 papers. Luna + Span-02 + Clef
  also types best among the open-weight options. That needs `screen_gate()` and `screen_voters()`
  to accept three votes.
- **Mammoth-1 is the best reproduction finder** (42 of 43) but too expensive to be the
  discarding voter.
- **Before tuning further, re-read two labels.** `SB0319` and `goldpos_flora:GP685` are lost by
  every combination.
- **Gaps of 1–3 lost are within noise.** Prefer whichever combination is more robust to its
  cut-off and to its host.

## Limiting spend

Yes, and for every combination above. Spend can be capped in three places.

1. **In the pipeline: `LLM_DAILY_USD_BUDGET`** in `.env` (#220). This is a dollar cap per UTC day
   across OpenAI, Gemini and OpenRouter.
   - It counts usage already recorded that day.
   - OpenRouter calls count at the dollars OpenRouter reports billing; direct calls count at list
     price.
   - On #219's branch the decision-model transport checks it before every attempt too.
   - When it is reached the run stops cleanly. Verdicts already written stay, and nothing is
     recorded as a discard.
   - It defaults to `0`, which means off, so it must be set: for example
     `LLM_DAILY_USD_BUDGET=5` with `OPENAI_DAILY_TOKEN_BUDGET=0`.
   - Calls already in flight can overshoot it slightly.
2. **Per call.** An LLM voter's answer is capped at `JSON_MAX_OUTPUT_TOKENS` (16,384). A decision
   model produces no output, so its per-call cost is fixed by the input. The production request
   truncates abstracts at 4,000 characters.
3. **At the provider, as a hard backstop.**
   - **OpenRouter:** a credit limit per API key, set in its dashboard. This covers DeepSeek and
     every decision model.
   - **OpenAI:** a project budget, which covers Luna.

   These are account settings for the key holder, not code.

**What a cap buys at these prices.** At #219's $0.092 per 1,000 papers, $1 screens about 11,000
papers, and re-screening the whole backlog of ~33,600 costs about $3. The most expensive gate in
the table, at $0.55 per 1,000, would cost about $18 for the same backlog.

## Not measured

- DeepSeek V4 Pro, about $3–6 per 1,000 papers.
- The DeepSeek V4.1 Flash 10-17 snapshot, about $1.80 for a full run.
- Liquid d1-ultra, Drex Max, ScaleDown Classify-1, Mercury Decide and Clef Omni.
- Upstage Solar Decide, which reported 0% uptime.
- DeepSeek + gpt-5.4-mini, the pair before main. It has no answers on this set.
