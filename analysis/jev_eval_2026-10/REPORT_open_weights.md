# Open-weight decision models beside Luna (2026-10-28)

Perplexity has withdrawn two Decider versions in October, each within days of its
successor (`REPORT_v12.md`). A decision voter whose weights are published cannot be
withdrawn in that way: any host can serve the same checkpoint, and it can be
self-hosted. Six open-weight models from OpenRouter's decision catalog were run on the
same 2,186 cases with the production request (`run_open.py`; `score_open.py` →
`score_open_out.txt`). Spend: $0.99.

## Result

**Span-02 (Apache-2.0) is the one to try, with Cloudflare Clef (27B, Apache-2.0) as the
alternative.** On the negatives humans labelled the two are level with each other and
with the current pair, and Span-02 costs a fifth of Clef. Clef's lower miss count rests
on a cut-off placed just below a cluster of positives, whereas Span-02's does not move
with the cut-off. The four small models are clearly behind.

Each voter beside Luna, discarding only when both say `none`; the cut-off is each
model's own, chosen on this set:

| voter | weights | $ per 1,000 works | cut-off | positives lost /1,456 | hard discarded /344 | curated discarded /386 | type correct (override rule) | reproductions found /43 |
|---|---|--:|--:|--:|--:|--:|--:|--:|
| *DeepSeek V4 Flash (current, LLM)* | MIT | *0.091* | | *8* | *262 (76%)* | *329 (85%)* | *620/635 (97.6%)* | *39* |
| *Perplexity v1.1 (withdrawn)* | closed | *0.043* | *0.4* | *2* | *258 (75%)* | *314 (81%)* | *629/641 (98.1%)* | *38* |
| *Perplexity v1.2* | closed | *0.043* | *0.4* | *5* | *252 (73%)* | *309 (80%)* | *624/638 (97.8%)* | *34* |
| **Clef 27B** | Apache-2.0 | 0.121 | 0.5 | **3** | 259 (75%) | 316 (82%) | **628/639 (98.3%)** | **39** |
| Clef 27B | Apache-2.0 | 0.121 | 0.6 | 8 | 265 (77%) | 328 (85%) | | |
| Span-02 (26B, 4B active) | Apache-2.0 | 0.022 | 0.7 | 5 | 260 (76%) | 309 (80%) | 624/638 (97.8%) | 39 |
| Clef Flash 9B | Apache-2.0 | 0.048 | 0.6 | 4 | 247 (72%) | 288 (75%) | 625/639 (97.8%) | 39 |
| Drex v1.5 9B | custom | 0.083 | 0.7 | 4 | 253 (74%) | 304 (79%) | 623/640 (97.3%) | 32 |
| Tev1 4B | none stated | 0.092 | 0.6 | 4 | 246 (72%) | 296 (77%) | 620/640 (96.9%) | 26 |
| Kev 4B | Apache-2.0 | 0.087 | 0.5 | 5 | 240 (70%) | 263 (68%) | 623/640 (97.3%) | 32 |

What decides the gate is how a voter ranks the 625 works Luna calls `none` (17 positives,
608 negatives): AUC 0.943 for v1.1, 0.931 v1.2, 0.914 Clef, 0.910 Span-02, 0.885 Clef
Flash, 0.864 Tev1, 0.862 Drex, 0.837 Kev.

**Clef** at 0.5 misses 3 positives against the current pair's 8, for 1 and 3 points
less discard; at 0.6 it matches the current pair (8 misses, 77% / 85%).
Its Hugging Face history since release (2026-10-01) is model-card edits only, the card
gives an SGLang launch command, and OpenRouter serves it from three hosts —
PrimeIntellect ($0.042/M), DeepInfra ($0.192/M, fp8) and Cloudflare ($0.24/M). This run
was routed across them; on the cheapest host it would cost about $0.09 per 1,000 works,
the DeepSeek voter's price. Its OpenRouter id carries no date, so pinning means pinning
the host or self-hosting the published revision. With a confident Luna, P ≥ 0.9 and a
type at the 0.7 share open the title-search shortcut for 1,179 positives and 11 hard
negatives (DeepSeek + Luna: 1,363 and 46).

**Span-02** is half of v1.1's price and confident in both directions: its curve is
flat (4 of its 5 misses are below P = 0.1), so raising the cut-off buys discard without
more misses. Built on Gemma 4 26B-A4B (4B active), it is light to self-host; OpenRouter
has one host (Respan), which kept Span-01 online after Span-02's release.

## By label source

Only 161 of the 730 negatives were labelled by people: the validators' rejections and
the hand-coded screening set. The other 569 are papers an earlier screen discarded or
an older pipeline's LLM labelled negative, and agreement with them measures agreement
with those systems. On the human-labelled negatives the candidates converge:

| beside Luna | positives lost /1,456 | human-labelled negatives discarded /161 | pipeline-labelled discarded /569 |
|---|--:|--:|--:|
| *DeepSeek V4 Flash (current)* | *8* | *68%* | *85%* |
| Perplexity v1.1 at 0.4 | 2 | 67% | 82% |
| Clef at 0.5 | 3 | 68% | 82% |
| Span-02 at 0.7 | 5 | 68% | 81% |

The paired difference in misses between Span-02 at 0.7 and Clef at 0.5 is 3 against 1
(exact McNemar p = 0.62). Clef's probabilities are spread (71% of the negatives Luna
calls `none` sit between 0.1 and 0.9, against 16% for Span-02), and five of the 17
positives Luna misses lie within 0.1 of Clef's cut-off: at 0.6 its misses rise from 3
to 8. Only one lies within 0.1 of Span-02's.

## Limits

As in `REPORT_pplx.md`: every cut-off here is in-sample; one run per model; the gate's
positive side rests on the 17 positives Luna misses, so differences of one to three
misses are within noise; the curated negatives carry an earlier pipeline's labels.
Type truth is read live from the validation tables (642 positives).
