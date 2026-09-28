# LLM cost review (2026-09-25)

Upcoming campaign: ~19k-work screen + ~12k-work extraction. ≈$35–50 at today's behaviour,
≈$20 with the two transport fixes in `cost_levers.patch` (no answer, cache key or
generation changes).

## Per call site (measured 2026-09-21..25, from LLM cache entries + OpenRouter cost lookups)

| Call site (model) | Per call | $ per call | Campaign |
|---|---|---|---|
| Screen voter 1 (deepseek-v4-flash, low) | ~2,680 in (60% cached), ~775 out (reasoning) | $0.00015 Baidu; $0.0009–0.0022 Relace | 19k: $2.9 capped, $5–16 uncapped |
| Screen voter 2 (gpt-6-luna, low) | ~2,500 in (76% cached), ~300 out | $0.00011 flex / $0.00022 std | $2.1 / $4.1 |
| Luna extraction, all sites | ~12k in (~45% cached), ~2.2k out per work | $0.0009 flex / $0.0018 std per work | 12k: $11 / $22 |
| Pick check (deepseek-v4.1-flash, low) | ~3.4k in, ~1.65k out, ~0.28/work | $0.00075–0.0015 | $2.5–5 |
| PDF parse (gemini-3.1-flash-lite) | rare fallback | — | < $0.10 |

Stale constants: `EXTRACT_PDF_PARSE_USD = 0.012` (far too high), `EXTRACT_RUNG_TOKENS`
(~700 out assumed, ~1.8k measured), `TIER_OUTPUT_TOKENS["screen_expensive"] = 300` (~1,075
measured). CLAUDE.md says voter 2 is gpt-5.4-mini; config says gpt-6-luna.

Unexplained: OpenRouter reports $14.89 this week; recorded tokens explain ~$3–5 (Relace-heavy
routing, or another consumer of the key). The patch's per-call `usd` recording will tell.

## Levers

| Lever | Saving | Changes answers? | Status |
|---|---|---|---|
| Cap deepseek-v4-flash hosts at $0.30/M output (OpenRouter price sort favours Relace, 7× Baidu per vote, fp4) | $2–13 | no | patch |
| `OPENAI_FLEX_PATIENCE`: wait and retry flex before standard (refusals come in waves: 90% on 09-24, 2% evening 09-23) | up to ~$13 | no | patch (default 0) |
| Record OpenRouter-reported cost per call (`usd`) | — | no | patch |
| Batch API for the Luna voter | ≈ flex | no | not done |
| LINKING_EFFORT medium → low | $2–4 | yes (new generation) | proposal only |
| Skip voter 2 when voter 1 qualifies | $2–4 | yes | not recommended |

Prompt order is already cache-friendly (rules first, paper last). DeepSeek caching works
only on some hosts and is worth < $0.5; DeepSeek's own API does not serve v4-flash.

## Recommendations
1. Apply `cost_levers.patch` once `shared/llm_client.py` is free; `/code-review`.
2. Run with `OPENAI_FLEX_PATIENCE=600`, preferably evening/night UTC; `OPENAI_DAILY_TOKEN_BUDGET=0`.
3. Read the `usd` totals after the first screen batch against the forecast (~$5 screen, ~$14 extraction).
