# Models and screening

[Documentation index](README.md) · [Setup](setup.md) · [Stage 2 flow](code-flow/stage2-filter.md)

## The classification model

**The classifier is a two-model vote in Stage 2:**

| Role | Exact model ID | Provider | Reasoning effort |
|------|----------------|----------|------------------|
| Screening voter 1 | `gpt-6-luna` | OpenAI direct | `low` |
| Screening voter 2 | `respan/span-02-20261015` (Span-02, decision model) | OpenRouter decisions endpoint | not applicable |

These are `SCREENING_MODEL_1` / `_2` and `SCREENING_EFFORT_1` / `_2` in
[shared/config.py](../shared/config.py). This page was checked against the code on
2026-10-28. The constants in that file determine new calls; they cannot be overridden
in `.env`. The tier name `screen_expensive` identifies this classifier.

Luna reads the title and abstract using `build_classify_prompt()` in
[shared/prompts.py](../shared/prompts.py) (v3.3) and returns `classification`
(`replication`, `reproduction`, `both`, `none`, or `unclear`), `confident`,
`categories`, `evidence_quote` and `reasoning`. Span-02 is asked the same rules as a
choice over the same five classes (`build_screen_decision()`) and returns one
probability per class and no text. `_decision_vote()` in
[shared/llm_client.py](../shared/llm_client.py) turns them into a vote: `none` below
P(qualifies) 0.7, confident at 0.9, and a type when one holds 0.7 of P(qualifies),
otherwise `unclear`. Its weights are open (Apache-2.0, `respanai/span-02`).

The decision rule is `screen_gate()` in [shared/llm_client.py](../shared/llm_client.py):

- **Discard:** both voters answer `none`, whatever their confidence.
- **Proceed:** any other complete pair, including disagreement and `unclear`.
- **Incomplete:** fewer than two answers. The work stays open for a retry and does
  not reach extraction. Each successful vote is cached, so retrying buys only the gap.

This is the **G-unanimous** gate. Confidence does not let a single voter discard a
paper. Passing the gate means the paper can be investigated further, not that a
human has validated it. For paper type, Luna's type stands unless Span-02 names a
different type, which then wins (`both` maps to replication); categories come from
Luna.

Stage 1 searches the OpenAlex snapshot. Stage 2 rules route candidates or discard
measured false positives; only the screening tier admits papers to Stage 3.
**Stage 3 reads the stored screening verdict and does not run the classifier again.**

## Other model roles

| Task | Configuration constant | Exact model ID | Effort | Status |
|------|------------------------|----------------|--------|--------|
| Link originals from abstract, references or full text | `LINKING_MODEL` | `gpt-6-luna` | `medium` | Stage 3 |
| Code outcomes for resolved originals | `OUTCOME_MODEL` | `gpt-6-luna` | `medium` | Stage 3 |
| Independently check a reference-list pick | `PICK_CHECK_MODEL` | `deepseek/deepseek-v4.1-flash` | `low` | Stage 3 |
| Extract references from a document when needed | `PDF_PARSE_MODEL` | `gemini-3.1-flash-lite` | `minimal` | Stage 3 fallback |
| Cheap pre-screen voter 1 | `PRESCREEN_MODEL_1` | `qwen/qwen3-30b-a3b-instruct-2507` | Not sent | Dormant |
| Cheap pre-screen voter 2 | `PRESCREEN_MODEL_2` | `mistralai/mistral-small-24b-instruct-2501` | Not sent | Dormant |

The reference-list checker uses **V4.1 Flash**; the screening voter uses **V4 Flash**.
Linking calls also code outcomes for their targets from the same reading. The
standalone outcome coder handles originals resolved by deterministic rules.

The cheap tier (`screen_cheap`) can only discard on two explicit noes; a keep passes
the work on to the main screen, and a failed call is no decision: the work stays open
and is asked again. All three routing specs for this tier
are currently `shadow: true`, so no live work reaches it. Its configured models are
not additional active classification voters. See
[activation requirements](filter-engine.md#activating-the-cheap-tier).

## Keys and running

- `OPENROUTER_API_KEY`: screening voter 2 and Stage 3's reference-list pick check;
  also the cheap tier if activated.
- `OPENAI_API_KEY`: screening voter 1, linking and outcome coding.
- `GEMINI_API_KEY`: document reference extraction when that fallback is needed.
- `SUPABASE_SERVICE_KEY` and `SUPABASE_URL`: shared claims and permanent verdicts
  for paid tier runs. The URL is supplied by `.env.defaults`.

A `/` in a model ID routes it through OpenRouter; an ID starting with `gemini`
routes to Google; other IDs route to OpenAI direct. Failed calls retry the same
model; there is no automatic substitute model.

After [setup](setup.md) and routing, inspect the main screening worklist and cost:

```bash
.venv/bin/python -m filter.engine screen --tier screen_expensive
# Add --run to claim work and make API calls.
```

See the [CLI reference](cli-reference.md) for release selection and other options.

## Evaluation evidence and existing results

**Both pairs were scored in October 2026 on 2,186 labelled works** (1,456 positives,
344 hard negatives and 386 curated negatives, including most of the earlier
screening-evaluation cases). Luna + Span-02 lost 5 positives and discarded 76% of hard
and 80% of curated negatives; the DeepSeek + Luna pair it replaced lost 8 and discarded
76% and 85%. On the 161 negatives people labelled, both discarded 68%
([`analysis/jev_eval_2026-10/REPORT_open_weights.md`](../analysis/jev_eval_2026-10/REPORT_open_weights.md)).
Perplexity's decision model was measured first and withdrawn twice in October
([`REPORT_v12.md`](../analysis/jev_eval_2026-10/REPORT_v12.md)). The positives are previously
identified replications (FLoRA entries, earlier evaluation cases and validated
pipeline rows), and the curated negatives are earlier-pipeline labels rather than
human judgements, so these figures do not estimate recall or the discard rate on the
production pool. On the earlier 390-case set, the DeepSeek + `gpt-5.4-mini` pair
measured one settled-positive miss and roughly 86–90% hard-negative discard across
two in-sample runs; see the [DeepSeek voter evaluation](../analysis/screening_eval/cheap_voter_2026-08.md)
and [v3.3 report](../analysis/screening_eval/report_v33.md).

Existing results can contain older models. The Luna + Span-02 screening generation
explicitly accepts settled DeepSeek + Luna and mini-era verdicts; extraction has its own
explicit generation equivalences. Neither switch rewrote all historical results.
Consult per-vote `model` fields in stored verdicts and exported `screen_votes`,
and extraction model provenance, to identify what actually graded a particular
paper. A current configuration constant alone cannot establish that history.

## Keeping this reference current

When changing a model, effort, prompt or screening gate, update this page and the
short answer in the root README in the same change. Check generation and cache
behaviour in [filter/engine/tiers.py](../filter/engine/tiers.py) and
[extract/tier.py](../extract/tier.py), and distinguish new evaluation evidence from
scores for earlier configurations. Keep dated reports as historical evidence.
