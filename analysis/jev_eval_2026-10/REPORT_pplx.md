# REPORT.md redone with Perplexity Decider v1.1 in Jev's place (2026-10-09)

Same 2,186 labelled works, same ds4 and luna votes, same G-unanimous gate as
`REPORT.md`. `perplexity/pplx-decider-v1.1-27b` was asked **Jev's exact `p7` question
and state** (`run2.py pplx_p7`) for a like-for-like swap; the full-rules `choice` run
of round 2 (`pplx_full`, `REPORT2.md`) is shown beside it. Scorer `score_pplx.py` →
`score_pplx_out.txt`. Spend for this step: $0.02.

## What Perplexity Decider is

A decision model on OpenRouter's `POST /api/alpha/decisions`: P(yes) for a `noul`
question, or a class plus probabilities for a `choice` question, and no text.
$0.02/M input, output free, 262k context, one host (Perplexity), **no prompt caching**
(`supports_implicit_caching: false`, no cache price). Released 2026-10-06; the v1 id
tested in the 2026-10-07 benchmark already returns 404.

## Result (G-unanimous: discard only when every voter says none)

| gate | positives lost /1456 | hard neg discarded /344 | curated neg discarded /386 |
|---|--:|--:|--:|
| ds4 + luna (**production**) | 8 (0.5%) | 262 (76%) | 329 (85%) |
| **pplx p7 < 0.5 + luna** | 6 (0.4%) | 253 (74%) | 307 (80%) |
| pplx p7 < 0.3 + luna | 4 (0.3%) | 245 (71%) | 281 (73%) |
| pplx p7 < 0.8 + luna | 10 (0.7%) | 264 (77%) | 328 (85%) |
| *jev p7 < 0.5 + luna* | *1 (0.1%)* | *215 (62%)* | *179 (46%)* |
| *jev p7 < 0.8 + luna* | *5 (0.3%)* | *248 (72%)* | *297 (77%)* |
| **pplx full < 0.4 + luna** | **2 (0.1%)** | 258 (75%) | 309 (80%) |
| pplx full < 0.5 + luna | 4 (0.3%) | 261 (76%) | 317 (82%) |
| ds4 + luna + pplx p7 < 0.8 veto | 6 (0.4%) | 260 (76%) | 319 (83%) |
| *ds4 + luna + jev p7 < 0.8 veto* | *3 (0.2%)* | *246 (72%)* | *292 (76%)* |
| pplx p7 < 0.5 + ds4 | 8 (0.5%) | 269 (78%) | 317 (82%) |
| pplx p7 < 0.5 alone | 18 (1.2%) | 299 (87%) | 330 (85%) |
| *jev p7 < 0.5 alone* | *5 (0.3%)* | *245 (71%)* | *183 (47%)* |

AUC (positives vs hard / curated negatives): pplx p7 **0.975 / 0.993**, jev p7
0.970 / 0.986, pplx full **0.984 / 0.997**.

- **Perplexity ranks better but leans the other way.** Jev is calibrated toward
  "keep" (few positives lost, few negatives discarded); Perplexity toward "discard".
  At the same threshold Perplexity loses more positives and discards far more
  negatives; across thresholds it is the better separator.
- **Errors are less disjoint than Jev's.** Of the 8 positives ds4 AND luna discard,
  Jev scores 7 at ≥ 0.5; Perplexity p7 scores 4, Perplexity full 5. Of the 18
  positives pplx p7 < 0.5 discards, ds4 also rejects 8 and luna 6 — it is a less
  independent second opinion, so it is weaker as a veto-only third voter.
- As a **replacement** for ds4 beside luna, Perplexity is better than Jev: at the same
  positives lost (5–6), pplx p7 + luna discards more (74% / 80% vs 72% / 77%) for less.
- The **full prompt is worth it on Perplexity** (unlike Jev): 0.984 vs 0.975 AUC, and
  `luna + pplx full < 0.4` loses 2 positives at near-production discard.

## Cost

| voter | in tok/call | output | caching | $ per 1k works |
|---|--:|---|---|--:|
| ds4 | 2,586 | 566 tok | automatic, 77% | 0.091 |
| luna | 2,568 | 174 tok | explicit prefix, 88% | 0.070 |
| jev p7 | 714 | free | none | 0.030 |
| **pplx p7** | 485 | free | none | **0.0097** |
| pplx full | 2,092 | free | none | 0.042 |

| screen | every work | cascade (2nd voter only when Luna says none, 29%) |
|---|--:|--:|
| ds4 + luna (production) | $0.161 | — |
| luna + pplx p7 | $0.080 (−50%) | $0.073 |
| luna + pplx full | $0.112 (−30%) | $0.082 |
| luna + jev p7 | $0.100 | $0.079 |

## Title-search gate (step 4.6, works allowed the shortcut)

| rule | positives /1456 | hard neg /344 | curated neg /386 |
|---|--:|--:|--:|
| today: ds4 and luna both confident | 1,363 | 46 | 13 |
| luna confident + pplx full ≥ 0.9 | **1,379** | **20** | 6 |
| luna confident + pplx p7 ≥ 0.9 | 1,351 | 16 | 2 |
| *luna confident + jev ≥ 0.9* | *1,370* | *21* | *8* |

## Record type

ds4 and luna gave conflicting types on 24 positives. Perplexity full sides with luna
on 13 and with ds4 on 11, so it does not settle the conflict. With luna as the only LLM,
luna decides the type and Stage 3's `record_type_check` still corrects it.

## Recommendation

1. **Screen = luna + Perplexity full, discard when luna says none AND P < 0.4–0.5.**
   2–4 positives lost (production: 8), discard 75–76% / 80–82%, $0.112 per 1k
   (−30%). Ask Perplexity on every work (the user's rule); the cascade would save
   only $0.03 per 1k.
2. Cheapest acceptable: luna + Perplexity p7 < 0.5 — 6 lost, 74% / 80%, $0.080 (−50%).
3. Do not use Perplexity as a veto-only third voter; Jev is better in that role, and
   the user ruled out three models.
4. Title-search gate: luna confident AND Perplexity ≥ 0.9.
5. Shadow first. Pin the dated id `perplexity/pplx-decider-v1.1-27b-20261006`; a 404
   must be an incomplete screen, never a discard.

## Follow-up 2026-10-10: the easy-negative leak, and type coding

**Leak** (`easyneg_out.txt`). luna + pplx full < 0.4 keeps 77 of 386 curated negatives
(production 57). 47 are kept by luna, 62 by Perplexity; the 30 that only Perplexity
keeps are: ~8 named vocabulary traps the rules exclude (viral / DNA replication,
two-stage GWAS "replication cohorts"), ~8 doubtful labels ("Replication of X (2008)"
proposal papers, self-extensions), ~14 plain original studies scored 0.40–0.80. The
curated negatives are OLD-PIPELINE LLM labels ("old-pipeline false_positive"), not
human ones. Moving the named exclusions into the `none` criterion (`pplx_full2`)
lowers the trap scores (NC075 0.45 → 0.23, NC145 0.48 → 0.25) but moves the totals
by one point: luna + pplx_full2 < 0.4 → 2 lost, 75% / 81%; < 0.6 → 6 lost, 76% / 85%
(production: 8 lost, 76% / 85%). Perplexity reads long rule text less literally than
an LLM; the rest of the leak sits in its 0.4–0.8 band.

**Type** (`type_eval.py` → `type_eval_out.txt`). Truth = validators' type on 645
positives (602 replication, 43 reproduction).

| coder | correct | reproductions found | replications called reproduction |
|---|--:|--:|--:|
| luna | 611/631 (96.8%), 14 untyped | 33/43 | 12 |
| today (ds4 first, else luna) | 621/638 (97.3%) | 39/43 | 14 |
| **pplx full choice** | **633/645 (98.1%)** | **39/43** | **8** |
| luna, pplx overrides at share ≥ 0.7 | 632/645 (98.0%) | 38/43 | 8 |

Perplexity's own `choice` answer codes the type best, from the same call; an override
rule or a second call adds nothing.

## Limits

In-sample thresholds; one run per voter; one host; model two days old; the positives
are mostly easy; no evidence quote or reasoning from Perplexity.
