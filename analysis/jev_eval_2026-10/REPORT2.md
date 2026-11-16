# Round 2 — DeepSeek v4.1, decision models (Jev, Perplexity), minimal prompts, cheap tier (2026-10-08)

Same 2,186 labelled works as `REPORT.md` (1,456 positives, 344 hard negatives, 386
curated negatives). Runner `run2.py`, scorer `score2.py` → `score2_out.txt`; the
pre-screen view with production bypasses → `prescreen_out.txt`. Spend ≈ $1.50.

Decision models go through `POST openrouter.ai/api/alpha/decisions` (the endpoint and
`choice` question shape of the 2026-10-07 decision benchmark). Perplexity's
`pplx-decider-v1-27b`, tested in that benchmark, now returns 404; its successor
`perplexity/pplx-decider-v1.1-27b` (released 2026-10-06) is what was run.

| voter | what it is asked | in tok/call | $ per 1k works |
|---|---|--:|--:|
| ds4 (prod voter 1) | v3.3 screen prompt | 2,586 | 0.091 |
| ds41 | v3.3 screen prompt on deepseek-v4.1-flash @ low | 2,620 | **0.459** |
| luna (prod voter 2) | v3.3 screen prompt | 2,568 | 0.070 |
| jev (round 1, `p7`) | short definition + one yes/no question | 714 | 0.030 |
| jev_full | `choice` of 5 classes + FULL production rules | 2,385 | 0.100 |
| pplx_full | same as jev_full | 2,092 | 0.042 |
| jev_min | one-sentence yes/no, abstract cut to 800 chars | 476 | 0.020 |
| pplx_min | same as jev_min | 281 | 0.006 |
| pplx_minfa | one-sentence yes/no, whole abstract | 438 | 0.009 |
| qwen + mistral (cheap tier, mistral only after qwen "no") | `p7` | 432 / 587 | 0.032 |

"Full rules" = `_CLASSIFY_PROMPT` from WHAT QUALIFIES to the answer instructions; only
the generative output instructions (evidence quote, reasoning, JSON, confidence field)
are removed. The round-1 `p7` Jev question had none of the inclusion/exclusion rules,
which is why it was 714 tokens.

## Accuracy (G-unanimous: discard only when every voter says none)

| gate | positives lost /1456 | hard neg discarded | curated neg discarded | $ per 1k |
|---|--:|--:|--:|--:|
| ds4 + luna (**production**) | 8 | 76% | 85% | 0.161 |
| ds41 + luna | 10 | 74% | 78% | 0.529 |
| **pplx_full < 0.5 + luna** | **4** | 76% | 82% | 0.112 |
| pplx_full < 0.3 + luna | 2 | 74% | 77% | 0.112 |
| **pplx_minfa < 0.3 + luna** | 6 | 72% | 83% | **0.079** |
| jev_full < 0.5 + luna | 5 | 75% | 84% | 0.170 |
| jev < 0.8 + luna (round 1) | 5 | 72% | 77% | 0.100 |
| pplx_full < 0.5, alone | 8 | 82% | 87% | 0.042 |
| jev_full < 0.3, alone | 8 | 80% | 85% | 0.100 |
| pplx_minfa < 0.1, alone | 4 | 73% | 70% | 0.009 |
| jev_min < 0.1, alone | 11 | 39% | 21% | 0.020 |

AUC (positives vs hard / curated negatives): pplx_full 0.984 / 0.997, pplx_minfa
0.981 / 0.995, jev_full 0.975 / 0.993, jev (p7) 0.970 / 0.986, pplx_min 0.972 / 0.985,
jev_min 0.957 / 0.973.

**Categorical type.** On the 1,308 positives where ds4 and luna agree on a type,
pplx_full gives the same type on 1,289 (98.5%) and finds 37/39 reproductions; jev_full
1,275 (97.5%), 36/39. A decision model can supply `record_type`.

## Pre-screen (discard alone, before the expensive screen)

With the production bypasses (`prescreen_bypass`: replication phrase in the text, or
abstract < 200 chars — 1,420 of 1,456 positives are bypassed in this set, so "raw"
losses are the honest safety measure):

| pre-screen | positives lost raw | lost after bypass | hard neg discarded | curated neg discarded | $ per 1k |
|---|--:|--:|--:|--:|--:|
| cheap tier (qwen + mistral) | 18 | 7 | 44% | 7% | 0.032 |
| **pplx_minfa < 0.05** | **1** | **0** | 33% | 5% | **0.009** |
| pplx_minfa < 0.1 | 4 | 2 | 39% | 7% | 0.009 |
| jev_min < 0.1 | 11 | 2 | 21% | 4% | 0.020 |

Without bypasses, pplx_minfa < 0.05 loses 1 positive and discards 60% / 45%.

## Conclusions

1. **DeepSeek v4.1-flash: no.** More positives lost (10 vs 8), fewer negatives
   discarded, 5× the cost of v4 ($0.459 vs $0.091 per 1k; it reasons more and its
   fp8 hosts bill more).
2. **Perplexity Decider beats Jev in every role**: higher AUC, 2–5× cheaper, fewer
   tokens. Jev is not needed.
3. **Replace DeepSeek with Perplexity, keep Luna.** pplx_full + luna halves the
   positives lost (8 → 4) at the same discard and −30% screen cost; the minimal
   pplx_minfa + luna is −51% for 6 lost. Luna keeps the quote / reasoning /
   confidence fields Stage 3 reads.
4. **Drop the qwen + mistral cheap tier.** pplx_minfa < 0.05 loses 1 positive raw
   (vs 18) for 3.5× less money. For a large noisy pool it is a usable pre-screen.
5. **≤ 500 tokens costs Jev its accuracy, not Perplexity.** Jev adds ~270 tokens of
   its own per call, so 500 tokens means cutting the abstract. Perplexity counts
   ~440 tokens with the whole abstract.

## Limits

- Thresholds read in-sample; one run per voter.
- `pplx-decider-v1.1-27b` is two days old, has one host, and its v1 predecessor
  disappeared within days of the benchmark. Pin the dated id, and treat a 404 as an
  incomplete screen (never as a discard).
- The positives are mostly easy (most carry an explicit replication phrase).
- Decision models return no evidence quote or reasoning.

## Reproduce

```bash
.venv/Scripts/python.exe analysis/jev_eval_2026-10/run2.py ds41 jev_full pplx_full jev_min pplx_min pplx_minfa qwen mistral
.venv/Scripts/python.exe analysis/jev_eval_2026-10/score2.py
```
