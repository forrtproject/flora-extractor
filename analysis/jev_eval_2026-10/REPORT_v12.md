# Perplexity Decider v1.2 against v1.1 (2026-10-28)

`perplexity/pplx-decider-v1.1-27b-20261006`, the decision voter this branch is built on,
no longer has an OpenRouter endpoint. Perplexity released **Decider V1.2** (27B, same API
contract, same price) and **Decider Flash V1.2** (4B, half the price) on 2026-10-16 and
withdrew v1.1, as it withdrew v1 within days of v1.1's release on 2026-10-06. Both v1.2 models
were run on the same 2,186 cases with the production request (`run_v12.py`;
`score_v12.py` → `score_v12_out.txt`). Spend: $0.15.

## Result

**v1.2 is not a drop-in replacement for v1.1.** At every cut-off from 0.3 to 0.6 it
loses more positives than v1.1 and discards fewer hard negatives (curated negatives are
level within one case at 0.5 and 0.6); it finds fewer reproductions; and it opens the
title-search shortcut for fewer positives. Flash v1.2 is worse than v1.1
on every measure except price.

Each voter beside Luna, discarding only when both say `none`, at the shipped cut-off 0.4:

| voter | positives lost /1,456 | hard negatives discarded /344 | curated negatives discarded /386 | AUC, hard / curated | title shortcut: positives / hard negatives | type correct (shipped rule) | reproductions found /43 | $ per 1,000 works |
|---|--:|--:|--:|--:|--:|--:|--:|--:|
| v1.1 | 2 | 258 (75%) | 314 (81%) | 0.984 / 0.997 | 1,364 / 19 | 629/641 (98.1%) | 38 | 0.043 |
| v1.2 | 5 | 252 (73%) | 309 (80%) | 0.988 / 0.996 | 1,298 / 10 | 624/638 (97.8%) | 34 | 0.043 |
| Flash v1.2 | 5 | 249 (72%) | 293 (76%) | 0.974 / 0.978 | 1,015 / 7 | 622/638 (97.5%) | 37 | 0.021 |
| *DeepSeek V4 Flash + Luna (production)* | *8* | *262 (76%)* | *329 (85%)* | | *1,363 / 46* | *620/635 (97.6%)* | *39* | *0.091 (DeepSeek)* |

Luna adds $0.070 per 1,000 works to each pair.

**Paired.** v1.2 loses three positives that v1.1 kept (two at P(qualifies) = 0.39, one at
0.29) and none the other way (exact McNemar p = 0.25). Among negatives Luna calls `none`,
v1.1 discards 18 that v1.2 keeps and v1.2 discards 7 that v1.1 keeps (p = 0.04). v1.2
ranks as well as v1.1 overall (AUC), but it is less confident (median P(qualifies) on
positives 0.993 against 0.997), so the cut-offs tuned on v1.1 do not carry over, and no
cut-off recovers v1.1's combination: at < 0.3 it loses 3 positives but discards 69% / 75%,
and at < 0.6 it loses 7 at 75% / 86%. Of the 71 positives that lose the title shortcut,
17 still have P ≥ 0.9 but no type holding the 0.7 share.

**Truncation.** The production request's 4,000-character abstract and the whole abstract
give the same results within two hard negatives (the `eval` rows in `score_v12_out.txt`,
which repeat the 51 long cases whole). The whole abstracts `pplx_full2` was sent did not
move its numbers.

## Against the current pair

Luna + v1.2 at 0.4 loses 5 positives against DeepSeek + Luna's 8, but discards 3 and 5
points fewer hard and curated negatives; at 0.6 it is level with DeepSeek + Luna (7 lost,
75% / 86%). It types about as accurately (97.8% against 97.6%) but finds 34 of 43
reproductions against 39.

## Limits

As in `REPORT_pplx.md`: cut-offs are in-sample (tuned on v1.1), one run per model, the
positives are previously identified works, and the curated negatives carry an earlier
pipeline's labels. Type truth is read live from the validation tables: 642 positives
today against 645 in the October run.
