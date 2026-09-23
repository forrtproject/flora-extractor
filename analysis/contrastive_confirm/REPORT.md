# Contrastive confirmation of the reference-list pick (handover step 5, 2026-09-23)

`run.py` buys and caches the answers (`out/`, content-keyed), `analyze.py` scores them.
Cases: the 229 `replay_pick.py` cases with a cached prompt — 56 judged-wrong, 10
judged-right (`ours`/`both`), 13 split, 150 controls. Spend ≈ $1.16.

**Models (OpenRouter, 2026-09-23):** `deepseek/deepseek-v4.1-flash` ($0.10/$0.50 per 1M,
≈$0.0012/call) and `z-ai/glm-5.3-flash` ($0.15/$0.50, ≈$0.0005/call).

**Framings.** *Blind*: "which record on this list is the original — or can't tell / not on
the list"; flagged when the pick is not among those named. *Contrast*: "here is the pick and
the alternatives; does the evidence single out the pick?"; flagged on "no".

## 1. Against the picks we shipped

| Run | Wrong flagged /56 | …names the judges' original | Controls flagged /150 | Judged-right flagged /10 |
|---|---:|---:|---:|---:|
| DeepSeek low, blind | **31** | **23** | **2** | **0** |
| DeepSeek medium, blind | 32 | 23 | 2 | 1 |
| DeepSeek medium, contrast | 16 | 12 | 3 | 3 |
| GLM high, blind | 28 | 22 | 4 | 4 |
| GLM medium, blind | 27 | 15 | 5 | 5 |
| GLM medium, contrast | 15 | 10 | 3 | 3 |
| Blind, DeepSeek AND GLM | 22 | 13 | 0 | 1 |
| Blind, DeepSeek OR GLM | 37 | 25 | 7 | 5 |

Blind catches twice what contrast does — showing the pick anchors the checker, the current
check's weakness. DeepSeek beats GLM; low effort = medium. DeepSeek's `confident` is always
true (uninformative). The 2 control flags are debatable (ego-depletion multilab: Sripada 2014
protocol vs Baumeister 1998 effect; Kube 2018 vs 2019, same authors).

## 2. Adoption test — against the step-3 prompt's picks

Blind answers do not depend on the pick, so they score `analysis/pick_pilot/replay_newprompt.csv`
(and `_rep1`) with no new calls (old `@x1999b` keys mapped onto `@x1999_2`).

| Blind run | Repeated wrong picks caught /25 (names judges' original) | New prompt's correct switches wrongly flagged | Controls wrongly flagged |
|---|---:|---:|---:|
| DeepSeek low | 8 (6) | 2/9 | 1/141 |
| DeepSeek low, 2nd sample | 10 (7) | 2/7 | 1/143 |
| DeepSeek medium | 9 (6) | 1/9 | 1/141 |
| GLM high | 8 (5) | 2/9 | 2/141 |
| GLM medium | 8 (4) | 2/9 | 4/141 |

~15 of the 25 repeated wrong picks get past every model tried — with only abstract + reference
list the evidence genuinely points at the sibling (e.g. `@carter2010`, `@loughlin2007`,
`@mccracken2010`, `@lang2006`, `@varma2017`, `@iannone2014`, `@bauman2023`). In 4 of 25 the
judges' original is not on the list.

## Recommendation

DeepSeek v4.1 flash, blind framing, effort `low`, on every accepted `llm_references` link. On a
flag: withhold `match_certain` and descend to the full-text rung, keeping the checker's
alternative and reasoning in `link_evidence`. Do not swap in the checker's pick (it is the
judges' original in only 6–7 of 8–10 catches). Scale: at 5–8% wrong picks over ~1,570 works,
≈25–40 wrong originals caught vs ≈15 correct links sent the long way; ≈$2 total. Skip contrast,
GLM and model pairs.

## Open questions

1. Each answer bought once; low/medium agree on nearly every repeat, but no same-settings re-run.
2. Whether the full-text rung then fixes the flagged repeats is unmeasured — needs a sandbox run
   of step 3 + this check together.
3. Wiring needs a model constant in `shared/config.py`, a prompt builder (in
   `_GENERATION_PROMPTS` if it decides shipped fields) and the key map passed into
   `_confirm_keyed_row`, which it does not get today.
4. Re-run `analyze.py` (free) if the step-3 replay files change.
