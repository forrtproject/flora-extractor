# Models and screening

[Documentation index](README.md) · [Setup](setup.md) · [Stage 2 flow](code-flow/stage2-filter.md)

## The classification model

**The classifier is a two-model vote in Stage 2:**

| Role | Exact model ID | Provider | Reasoning effort |
|------|----------------|----------|------------------|
| Screening voter 1 | `deepseek/deepseek-v4-flash` | OpenRouter | `low` |
| Screening voter 2 | `gpt-6-luna` | OpenAI direct | `low` |

These are `SCREENING_MODEL_1` / `_2` and `SCREENING_EFFORT_1` / `_2` in
[shared/config.py](../shared/config.py). This page was checked against the code on
2026-10-28. The constants in that file determine new calls; they cannot be overridden
in `.env`. The tier name `screen_expensive` identifies this classifier.

Each voter reads the title and abstract using `build_classify_prompt()` in
[shared/prompts.py](../shared/prompts.py) (v3.3). Each returns `classification`
(`replication`, `reproduction`, `both`, `none`, or `unclear`), `confident`,
`categories`, `evidence_quote` and `reasoning`.

The decision rule is `screen_gate()` in [shared/llm_client.py](../shared/llm_client.py):

- **Discard:** both voters answer `none`, whatever their confidence.
- **Proceed:** any other complete pair, including disagreement and `unclear`.
- **Incomplete:** fewer than two answers. The work stays open for a retry and does
  not reach extraction. Each successful vote is cached, so retrying buys only the gap.

This is the **G-unanimous** gate. Confidence does not let a single voter discard a
paper. Passing the gate means the paper can be investigated further, not that a
human has validated it. For paper type, the first qualifying vote in voter order
wins (`both` maps to replication); categories are the union of the two votes.

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

The cheap tier (`screen_cheap`) can only discard on two explicit noes; a keep or
failure passes the work on to the main screen. All three routing specs for this tier
are currently `shadow: true`, so no live work reaches it. Its configured models are
not additional active classification voters. See
[activation requirements](filter-engine.md#activating-the-cheap-tier).

## Keys and running

- `OPENROUTER_API_KEY`: screening voter 1 and Stage 3's reference-list pick check;
  also the cheap tier if activated.
- `OPENAI_API_KEY`: screening voter 2, linking and outcome coding.
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

**The current DeepSeek + GPT-6 Luna pair has not yet been scored against the
screening evaluation set.** The earlier DeepSeek + `gpt-5.4-mini` pair measured one
settled-positive miss and roughly 86–90% hard-negative discard across two runs.
These were in-sample evaluations, and the gate was selected on the same cases.
Those figures do not establish the accuracy of the Luna pair or its discard rate
on the production pool. See the [DeepSeek voter evaluation](../analysis/screening_eval/cheap_voter_2026-08.md)
and [v3.3 report](../analysis/screening_eval/report_v33.md).

Existing results can contain older models. The screening generation explicitly
accepts settled mini-era verdicts after the Luna switch; extraction has its own
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
