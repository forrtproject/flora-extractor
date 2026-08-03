# Pilot arm screen — Stage 3 front door

Rows screened: 3000 (positive=1000, ambiguous=1000, concept=1000)

## Per-arm gate decisions

| Arm | n | proceed | discard | no-decision | api_error |
| --- | --- | --- | --- | --- | --- |
| A (positive) | 1000 | 370 (37.0%) | 630 (63.0%) | 0 (0.0%) | 0 (0.0%) |
| B (ambiguous) | 1000 | 131 (13.1%) | 869 (86.9%) | 0 (0.0%) | 0 (0.0%) |
| C (concept) | 1000 | 307 (30.7%) | 693 (69.3%) | 0 (0.0%) | 0 (0.0%) |

## Arm A by is_reproduction

| is_reproduction | n | proceed | discard | no-decision | api_error |
| --- | --- | --- | --- | --- | --- |
| False | 516 | 213 (41.3%) | 303 (58.7%) | 0 (0.0%) | 0 (0.0%) |
| True | 484 | 157 (32.4%) | 327 (67.6%) | 0 (0.0%) | 0 (0.0%) |

## Every arm by type

| Arm | type group | n | proceed | discard | no-decision | api_error |
| --- | --- | --- | --- | --- | --- |---|
| A (positive) | article+preprint | 558 | 215 (38.5%) | 343 (61.5%) | 0 (0.0%) | 0 (0.0%) |
| A (positive) | dataset | 214 | 65 (30.4%) | 149 (69.6%) | 0 (0.0%) | 0 (0.0%) |
| A (positive) | other | 228 | 90 (39.5%) | 138 (60.5%) | 0 (0.0%) | 0 (0.0%) |
| B (ambiguous) | article+preprint | 600 | 63 (10.5%) | 537 (89.5%) | 0 (0.0%) | 0 (0.0%) |
| B (ambiguous) | dataset | 151 | 33 (21.9%) | 118 (78.1%) | 0 (0.0%) | 0 (0.0%) |
| B (ambiguous) | other | 249 | 35 (14.1%) | 214 (85.9%) | 0 (0.0%) | 0 (0.0%) |
| C (concept) | article+preprint | 511 | 64 (12.5%) | 447 (87.5%) | 0 (0.0%) | 0 (0.0%) |
| C (concept) | dataset | 324 | 226 (69.8%) | 98 (30.2%) | 0 (0.0%) | 0 (0.0%) |
| C (concept) | other | 165 | 17 (10.3%) | 148 (89.7%) | 0 (0.0%) | 0 (0.0%) |

## Screened record_type among proceeders

| Arm | replication | reproduction | (none) |
| --- | --- | --- | --- |
| A (positive) | 207 | 141 | 22 |
| B (ambiguous) | 28 | 19 | 84 |
| C (concept) | 29 | 5 | 273 |

## Empty abstracts

| Arm | empty abstract | of which proceed | of which discard |
| --- | --- | --- | --- |
| A (positive) | 111 | 45 | 66 |
| B (ambiguous) | 324 | 84 | 240 |
| C (concept) | 294 | 167 | 127 |

## Keyword verdict vs screen gate

| Arm | verdict_keyword | n | proceed | discard |
| --- | --- | --- | --- | --- |
| A (positive) | positive | 1000 | 370 | 630 |
| B (ambiguous) | ambiguous | 1000 | 131 | 869 |
| C (concept) | negative | 1000 | 307 | 693 |

## Tokens and cost (day 2026-08-03, flex halved)

| provider | model | in | out | $ |
| --- | --- | --- | --- | --- |
| gemini | gemini-3.5-flash-lite | 7,727,462 | 265,430 | $1.491 |
| openai | gpt-5.4-mini | 7,637,958 | 462,911 | $3.906 |
| **total** | | | | **$5.40** |

## Run configuration

- Voters: `gemini-3.5-flash-lite` (voter 1) + `gpt-5.4-mini` (voter 2, OpenAI direct),
  as returned by `screen_voters()`. Prompt and gate are the production
  `classify_replication()` / `screen_gate()`.
- Flex: `GEMINI_USE_FLEX=true` from `.env`; `OPENAI_USE_FLEX` is unset in `.env`, so it
  was exported as `true` for this run. No `service_tier` rejection warning appeared in
  the log for either provider, so both were served at flex and the prices above are
  halved accordingly.
- `OPENAI_DAILY_TOKEN_BUDGET` was raised to 20M for the run (the 8M default would have
  stopped it mid-way at ~7.6M OpenAI tokens); the $10 spend guard in the runner was the
  operative ceiling and was never approached.
- API keys came from `~/.claude/api_keys.env` (the repo `.env` holds no keys).
- 2 rows returned a one-vote (partial) screen on the first pass — a Gemini failure, not
  a verdict, and not cached. They were re-run and both completed. Final api_error count: 0.

## What the proceeds are made of

Most common (voter1, voter2) pairs among proceeders (`!` = confident, `?` = not):

- Arm A: replication!/replication! 144, reproduction!/none! 70, reproduction!/reproduction! 40
- Arm B: unclear?/unclear? 43, unclear?/none? 37, replication!/replication! 10
- Arm C: unclear?/unclear? 229, unclear?/none? 41, replication!/replication! 11

Arm C's 30.7% proceed rate is almost entirely soft: 273 of its 307 proceeders carry no
screened `record_type`, and 229 are unclear/unclear at low confidence. 226 of the 307 are
`dataset` rows (69.8% of C's datasets proceed), half of them with no abstract at all —
the gate cannot discard a row on which neither voter will commit to "none".
