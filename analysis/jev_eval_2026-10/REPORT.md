# Jev vs DeepSeek vs Luna as screen voters — 2,186 labelled works (2026-10-06)

Follow-up to `analysis/jev_eval_2026-09` (389 cases) at 5.6× the size, with the
human validators' verdicts from Supabase as the main new label source.

## Cases (`build_cases.py` → `cases.json`)

| source | label | n |
|---|---|--:|
| Supabase human `type_check` (both humans / admin agree) | positive | 617 |
| Supabase human `type_check` = `not_validation` (a screen false positive) | hard negative | 102 |
| `screening_eval` settled positives / hard negatives | pos / hard neg | 313 / 59 |
| `prescreen_eval` gold positives (FLoRA + reproductions) | positive | 526 |
| `prescreen_eval` screen-confirmed negatives | hard negative | 183 |
| `prescreen_eval` curated negatives | easy negative | 386 |

1,456 positives, 344 hard negatives, 386 easy negatives. Deduped on DOI/title; 40
works where the two humans disagreed were dropped.

## Voters (`run.py`)

- ds4 / luna: the production vote (`_classify_once`, v3.3 prompt, effort low). The
  production vote cache was read first (489 ds4 hits, 0 luna hits); misses were bought.
- jev: `typesafe/jev-1.13`, the `p7` Noul question of the 2026-09 eval, one question
  per call; "none" when P(yes) < T.

## Result (`score.py` → `score_out.txt`), gate G-unanimous

| gate | positives lost /1456 | hard neg discarded /344 | easy neg discarded /386 |
|---|--:|--:|--:|
| ds4 + luna (**production**) | 8 (0.5%) | 262 (76%) | 329 (85%) |
| jev<0.5 + luna | 1 (0.1%) | 215 (62%) | 179 (46%) |
| jev<0.7 + luna | 3 (0.2%) | 238 (69%) | 255 (66%) |
| jev<0.5 + ds4 | 1 (0.1%) | 225 (65%) | 181 (47%) |
| ds4 + luna + jev<0.8 veto | 3 (0.2%) | 246 (72%) | 292 (76%) |
| ds4 + luna + jev<0.7 veto | 2 (0.1%) | 236 (69%) | 253 (66%) |
| ds4 alone | 16 (1.1%) | 286 (83%) | 355 (92%) |
| luna alone | 17 (1.2%) | 269 (78%) | 339 (88%) |
| jev<0.5 alone | 5 (0.3%) | 245 (71%) | 183 (47%) |

- Jev AUC 0.970 vs hard negatives, 0.986 vs easy negatives.
- Errors are disjoint again: of the 8 positives ds4 AND luna discard, Jev scores 7 at
  P ≥ 0.56 (one at 0.10). Of Jev's 5 misses at 0.5, ds4 and luna each reject only 1.
- Jev is calibrated toward "keep": it discards less than half of the easy, off-topic
  negatives at 0.5. Every negative it keeps is a work Stage 3 then pays to extract.

## Cost (measured; luna from tokens at flex list price)

| voter | in tok/call | out tok/call | input served from cache | $/call | $/1k works |
|---|--:|--:|--:|--:|--:|
| ds4 | 2,586 | 566 | 77% (automatic) | $0.000091 | $0.091 |
| luna | 2,568 | 174 | 88% (explicit prefix) | $0.000070 | $0.070 |
| jev | 714 | 0 (free) | none — not supported | $0.000030 | $0.030 |

Total spend of this eval ≈ $0.37. Jev has no provider prompt caching
(`supports_implicit_caching: false`, no cache price) — it does not need it, since its
input is 3.6× shorter and output is free. Our own on-disk response cache can store
Jev answers like any other call.

## Limits

- Jev returns only P(yes): no `record_type`, `confident`, `categories`,
  `evidence_quote` or `reasoning`. Stage 3 reads `screen_votes` confidence (gates
  `llm_title_search`) and the record type. So Jev cannot REPLACE ds4 or luna without
  losing those fields; it can only be added as a third, discard-veto voter.
- Thresholds are read in-sample. Single run per voter. Some human "positives" look
  doubtful (e.g. SB0505 "Biodiversity Soup II", a pipeline paper).
- The human negatives are screen false positives only; the eval has no works the
  production screen discarded that humans later found to be replications.

## Recommendation

Keep ds4 + luna. Add Jev as a third voter whose only power is a veto on discard
(discard only if ds4 AND luna say none AND Jev P < 0.7–0.8). Measured: misses 8 → 2–3,
hard-negative discard 76% → 69–72%, + $0.03 per 1k works. Run it in shadow first.
Replacing ds4 with Jev (jev<0.5 + luna) is the safest for recall (1 miss) but keeps
~40% more negatives for Stage 3 and drops the reasoning/confidence fields.

## Reproduce

```bash
.venv/Scripts/python.exe analysis/jev_eval_2026-10/build_cases.py
.venv/Scripts/python.exe analysis/jev_eval_2026-10/run.py jev ds4 luna
.venv/Scripts/python.exe analysis/jev_eval_2026-10/score.py
```
