# Jev as a screening classifier (#212), and DeepSeek caching / v4.1-flash (#211) — 2026-09-28

Two evals, both re-using the committed case sets and scoring rules, every voter re-run
today on the same rows (no cached votes existed for either set):

- **cheap path** — the dormant pre-screen tier's eval (`analysis/prescreen_eval`, prompt
  `p7`): 184 screen-confirmed negatives, 400 curated negatives, 567 gold positives.
- **expensive screen** — the voter eval of `analysis/screening_eval/cheap_voter_2026-08.md`:
  319 settled positives (F140 excluded), 70 hard negatives, gate G-unanimous. LLM votes
  go through `shared.llm_client._classify_once` (production prompt, schema, host routing,
  effort), bypassing the cache.

Spend: ≈ $0.48 in total (pre-screen $0.20 across five voters, screen $0.28).

## What Jev is

`typesafe/jev-1.13` (released 2026-09-17) is not a chat model. It is a "System One"
decision model: you send a `state` and typed questions, and get back probabilities —
a `noul` is P(yes) — and no text. $0.042/M input, **output free**, 32k context, one
host (TypeSafe), no caching, `supported_parameters: []`. It is reached at
`POST https://openrouter.ai/api/v1/systemone` with the OpenRouter key; **it cannot go
through `call_openrouter`**, which sends `response_format` with `require_parameters`
and would find no host. The question used (`jev.py`) is `p7` with its yes/no guidance
moved into Jev's `criteria`; a second, plainer phrasing (`direct`) is reported too.

## Hypothesis

1. **Decorrelated errors.** Jev is a trained, calibrated decision model, not a
   generative LLM, so where it is wrong should differ from where our LLM voters are
   wrong. An AND/unanimity gate is only as safe as its voters' errors are disjoint.
2. **A threshold instead of a vote.** A probability lets the operating point be set
   recall-first and moved without re-prompting.
3. **Cheaper per decision** where the alternative is a reasoning model: no output
   tokens are billed, so no thinking tokens either.

## Result 1 — cheap path (pre-screen), 1,151 rows

After the shipped bypasses (regex, short, curated). The regex override already covers
556 of the 567 positives, so **"raw" losses — before the override — are what measure a
voter's own safety.**

| voter | screen-neg discarded | curated-neg | positives it says "no" to (raw) | lost after override | $/1k rows |
| --- | --: | --: | --: | --: | --: |
| qwen + mistral (shipped pair, re-run) | 106/184 (58%) | 27/400 | 4 | 0 | ≈ $0.043 |
| qwen alone | 106/184 | 29/400 | 6 | 0 | $0.027 |
| mistral alone | 117/184 | 33/400 | 13 | 0 | $0.031 |
| **Jev `p7`, P < 0.5, alone** | **103/184 (56%)** | 28/400 | **0** | 0 | **$0.036** |
| Jev `p7`, P < 0.59 (picked on dev, **test** split) | 56/97 (58%) | 15/208 | **0**/269 | 0 | |
| gpt-5-nano (effort minimal) | 104/184 | 32/400 | **157** | **3** | $0.032 |
| gpt-4.1-nano | 115/184 | 36/400 | **88** | **1** | $0.047 |

- Jev alone matches the shipped pair's discard in one call instead of two, and never
  leans on the override: its lowest-scored gold positive is 0.53. AUC 0.999 against the
  screen-confirmed negatives.
- **The nano models are not usable here**: they reject 16–28% of real replications and
  lose papers even through the override. Jev is the same price per row and strictly safer.
- Jev is **not cheaper than the current cheap voters per row** — the abstract dominates
  the bill and all are ~$0.03/1k (Jev's $0.036 includes a second question; one question
  is ~15% less). It is cheaper than the *pair*, because it replaces two calls.
- Decorrelation: the 4 positives BOTH qwen and mistral reject score 0.57–0.89 on Jev;
  Jev's weakest positives are ones they accept. The `direct` phrasing loses this —
  it scores two of those four at 0.07 — so the `p7` wording is what buys the safety.

## Result 2 — expensive screen, 389 cases

| gate (G-unanimous) | settled misses /319 | hard-neg discard /70 | voter-1 $/vote |
| --- | --: | --: | --: |
| **ds4 + luna (production today)** | **4** (H002, F005, F045, F130) | 58 (83%) | $0.000205 |
| ds4.1 + luna | 4 (H002, H009, F005, F045) | 55 (79%) | $0.000342 |
| ds4 + luna + **Jev `p7` veto, P < 0.8** | **1** (F005) | 55 (79%) | + $0.000036 |
| ds4 + luna + Jev `p7` veto, P < 0.7 | 0 | 50 (71%) | + $0.000036 |
| Jev `p7` < 0.5 + luna | 0 | 38 (54%) | |
| Jev `p7` < 0.5 alone | 2 (H003, H006) | 43 (61%) | $0.000036 |

- **The production pair is unmeasured and this run puts it at 4 misses**, against 1
  (F005) for the pair `cheap_voter_2026-08.md` evaluated (ds4 + gpt-5.4-mini) —
  `shared/config.py` says of the Luna pair "That pair needs measurement". One run,
  and 1 vs 4 of 319 is not significant; it is the direction that matters for a
  discard gate, and it is worth a second run before the next screen campaign.
- H002, F045 and F130 are rejected **confidently** by both ds4 and luna; Jev scores them
  0.89, 0.92, 0.94. Jev's own misses (H003, H006, at 0.23/0.25) are confidently accepted
  by all three LLMs. The errors are disjoint — hypothesis 1 holds on this sample.
- Jev cannot **replace** a screen voter: it returns no `evidence_quote`, `reasoning`,
  `categories` or `confident`, which Stage 3 reads (`screen_votes` gates
  `llm_title_search`), and alone it discards far fewer hard negatives.

## #211 — DeepSeek caching and v4.1-flash

- **There is nothing to implement for caching.** DeepSeek and its OpenRouter hosts cache
  prompt prefixes automatically; there is no explicit-breakpoint API like the one used
  for Luna (OpenRouter's `cache_control` is Anthropic/Gemini only). The classify prompt
  is already rules-first (the static prefix is ~99% of the empty template) and the host
  ranker already prices cache reads. Measured today: **67%** of ds4 input served from
  cache (StreamLake), **89%** for ds4.1 (DeepInfra); `SCREEN_VOTE_TOKENS` assumes 60%.
  Headroom if ds4 hit 89%: ≈ $0.7 per 19k-work campaign.
- **Moving the screen voter to v4.1-flash does not help.** It reasons more (692 vs 556
  output tokens/vote) and its cheapest fp8 host bills $0.42/M output vs $0.18/M, so a
  vote costs 1.67× ($0.000342 vs $0.000205). Alone it is better calibrated (3 vs 6
  confident-`none` on positives) but paired with luna it has the same 4 misses and a
  lower discard (79% vs 83%). `PICK_CHECK_MODEL` is already on v4.1-flash.
- DeepSeek's own API now serves v4.1-flash (not v4) with native caching at $0.003/M, but
  bills $0.60/M output and reports no quantization, so the fp8 floor excludes it — and it
  would not be cheaper for this output-heavy call anyway.

## What this cannot tell you

- In-sample: the Jev thresholds (0.5 pre-screen, 0.7–0.8 veto) were read off the same
  rows they are scored on; only the pre-screen's dev/test check is out of sample.
- The gold positives are the easy ones (556/567 carry an explicit replication phrase).
- Jev is ten days old, has one host, and its probabilities are quantized to 0.01.
  Its docs list literal reading and adversarial content as known weaknesses.

## Recommendation

1. **Cheap path:** if the pre-screen tier is ever woken, make it Jev (`p7`, P < 0.5)
   rather than the qwen+mistral pair — in shadow first, as the tier already requires.
2. **Expensive screen:** try Jev as a **third, veto-only voter** (discard only if ds4 AND
   luna say `none` AND Jev P(`p7`) < 0.8), in shadow on the next screen campaign, before
   anything is discarded on it. ~$0.7 per 19k works.
3. **Independently of Jev:** re-run this screen eval once more for ds4 + luna before the
   next screen campaign; if it again misses 3–4, the Luna swap cost recall.
4. #211: close — caching is automatic and already in effect; keep the screen voter on
   v4-flash.

Wiring Jev in means one new call path (`POST /v1/systemone`, ~40 lines, pinned
`typesafe/jev-1.13`, content-keyed cache on model + question text + state), because it
does not fit `call_model`'s chat-completions dispatch.

## Reproduce

```bash
.venv/bin/python analysis/jev_eval_2026-09/score.py          # pre-screen tables, from out/
.venv/bin/python analysis/jev_eval_2026-09/score_screen.py   # screen tables, from out_screen/
# re-buy (resumable): run.py jev | run.py <openrouter-id> [--effort=minimal]; screen_run.py ds4 ds41 luna jev
```
