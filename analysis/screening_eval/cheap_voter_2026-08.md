# Voter 1 replacement and the unanimity gate — 2026-08-13

Evidence behind two changes shipped together: `SCREENING_MODEL_1` moved from
`gemini-3.5-flash-lite@minimal` to `deepseek/deepseek-v4-flash@low`, and
`screen_gate()` moved from G-softqual to G-unanimous (discard only when every vote
is `none`). Motivation: the Gemini screening bill (~$4.55/campaign with flex) was
paid out of pocket; DeepSeek serves the same load for roughly $2.

## Design

Candidates ran the production v3.3 prompt over the three v3.2/v3.3 case sets — 60
`human` (50 no / 10 yes), 30 `heldout` (20 no / 10 yes), 300 `flora` positives
(F140 adjudication-pending, excluded from miss counts) — and each candidate's votes
were paired with the CACHED `gpt-5.4-mini` v3.3 votes under the shipped gate. The
scorer reproduces `report_v33.md` exactly when fed the incumbent's own votes. All
cases are in-sample derivation data (see `README.md`); no out-of-sample positives
existed at evaluation time, so DeepSeek's headline condition was run twice to
measure run-to-run variance instead.

## Candidate results (pair-level, shipped G-softqual gate)

| Voter 1 | Settled misses /319 | Hard-neg discard /70 | Cost per 390 calls |
| --- | --- | --- | --- |
| gemini-3.5-flash-lite (v32 / v32r / v33 runs) | 0 / 1 / 1 | 88.6% / 85.7% / 84.3% | ~$0.11 |
| deepseek-v4-flash effort=none | 7 | 91.4% | $0.035 |
| deepseek-v4-flash effort=low, run 1 | 1 (F005) | 87.1% | $0.070 |
| deepseek-v4-flash effort=low, run 2 | 2 (F005, F086) | 90.0% | ~$0.07 |
| qwen3.5-flash-02-23 thinking off | 1 (F144) | 80.0% | $0.078 |

qwen fails on discard rate. DeepSeek at `none` fails disqualifyingly on misses: its
confident-`none` precision is 0.86 against 0.90–0.94 for every other evaluated
voter, and 6 of its 7 misses are cases the Gemini voter rescued with a confident
qualifying vote. The effort is therefore load-bearing, not a tunable.

## Why the gate changed with the voter

Every DeepSeek miss beyond F005 came through G-softqual's second clause — one
confident `none` discarding over an unconfident partner. That clause leans on the
confident voter's calibration on true positives, a per-model property a voter swap
silently changes. Confident-`none` calibration measured over these runs:

| Voter | Confident-`none` on 319 positives | Precision of confident-`none` |
| --- | --- | --- |
| gpt-5.4-mini | 4 | 0.94 |
| gemini-3.5-flash-lite @minimal | 5 | 0.93 |
| deepseek @low (runs 1/2) | 5 / 7 | 0.93 / 0.90 |
| deepseek @none | 10 | 0.86 |

At `low` the calibration matches the incumbent, so a per-model asymmetric gate is
not justifiable; removing the clause for every voter is. Offline re-scoring of the
recorded votes under G-unanimous:

| Ensemble | Gate | Misses | Hard-neg discard |
| --- | --- | --- | --- |
| incumbent + gpt | G-softqual (was shipped) | 1 | 59/70 |
| incumbent + gpt | G-unanimous | 1 | 58/70 |
| deepseek@low + gpt, run 1 | G-unanimous | 1 (F005) | 60/70 |
| deepseek@low + gpt, run 2 | G-unanimous | 1 (F005) | 63/70 |

G-unanimous with the new pair equals the incumbent's shipped configuration on
misses in both runs and beats it on discard, at half the price. Its cost is about
one extra pass-through per 70 hard negatives — a Stage 3 processing cost, borne
mostly by free OpenAI credits, where a false discard silently loses a real
replication. F005 is missed by every configuration that does not include a voter
confidently affirming it: gpt rejects it confidently and DeepSeek supports it only
unconfidently.

## Caveats

- All numbers are in-sample; the gate variant was additionally selected on the same
  cases it is scored on. The mitigations are the two independent DeepSeek runs, the
  calibration mechanism being understood, and the rule being conservative — it
  removes a discard power rather than adding one.
- Run 1 mixed two OpenRouter hosts mid-run (the price-sorted provider queued
  reasoning calls at 57–249 s each; the run was restarted on throughput routing).
  Production routing now pins `sort: "price"` with `preferred_min_throughput: 40`
  and `require_parameters: true`.
- Raw vote files, the runner (`eval_cheap.py`, an `eval_v3.py` adaptation with a
  `--reasoning-effort` flag) and the scorer live in the session scratch directory;
  the checked-in survivors of this eval are this report and the archived voter
  files under `archive/analysis/screening_eval/` from the v3.3 era.
