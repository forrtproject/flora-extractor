# Reference-list pick fix — prompt change, key suffix, 150-work pilot (2026-09-23)

Handover `docs/handover-observatory-campaign-2026-09.md`, steps 3.1–3.4 and 4. Sandbox
only; no live verdict was written.

## Headline — scored against blinded truth coding

The random pilot (150 non-validated `llm_references` works, seed 20260923,
`sample_150.txt`) was coded blind by ten coder agents (`truth/`: packets without the pick,
`coder_instructions.md`, one answer JSON per work). `score_truth.py` → `truth_scored.csv`:

| live pick (gpt-5.6-luna / 5.4-mini, old prompt) → sandbox pick (gpt-6-luna, new prompt, `_2` keys) | works |
|---|--:|
| right → right | 126 |
| right → declined (`target_pending`) | 5 |
| right → partial / wrong | 2 / 1 |
| wrong → right | 6 |
| wrong → partial / declined | 1 / 1 |
| wrong → wrong | 4 |
| partial → right / partial | 1 / 1 |
| not a replication (both) | 2 |

**Wrong originals shipped: 12 → 5 of 150 (8.0% → 3.3%).** The cost: 5 correct links lost
to declines (none reached a document), 1 new wrong. The live wrong rate on a random sample
(8%) matches the adjudication's 5–8% estimate. Several of the fixes came through
`llm_title_search` after the reference pick declined — the ladder, not the pick alone.
The title-prefix matching in `score_truth.py` is approximate; read the diff rows in
`truth_scored.csv` before quoting single cases.

The replay below says the PROMPT edit alone is within run-to-run noise on the adjudicated
wrong picks, so most of this gain is gpt-6-luna. It is still a gain a redo buys.

## What changed
- `shared/prompts.py` — `_TARGET_TASK` (shared by `build_target_outcome_prompt` and
  `build_repro_target_outcome_prompt`): two bullets after the `match_certain` rule — the
  five sibling failure modes; "evidence only DESCRIBES the original → `match_certain` only
  when exactly one listed record fits AND no other same-author record fits"; a key suffix
  is not the paper's citation letter. Dead `_FROZEN_VERSIONS` entries deleted.
  Versions b1f887ddcc91 → e2c602f14d34 and 01349eec56c7 → 117f2c544a69; no other prompt moved.
- `shared/target_keys.py` — `_suffix()`: `@abrahams1999_2`, `_3`, … instead of letters.
  Keys are validated by exact `key_map` lookup, so no regex changed.
- `extract/tier.py` — `_GENERATION_EQUIVALENCES["474e80e4c32a6dfa"]` = the previous chain
  plus `7dbb1e92452d8333`. Generation 7dbb1e92452d8333 → 474e80e4c32a6dfa.
- Tests: generation pin, `_2`/`_3` keys; full suite 1,900 passed, 6 skipped.
- Not changed: `author_year_candidate_keys()` still uses letter suffixes.

## Replay (`replay_newprompt.py`)
Rebuilds each cached prompt under the new builder (byte-identical self-check). 216 of 229
carry HEAD's task block; 13 (gpt-5.4-mini era) are swapped whole and flagged `older_block`.
Each prompt run twice, the old prompt once more as a noise baseline (`replay_4runs.csv`).

Judged-wrong (n = 56) — fixed / declined / still wrong:
- old prompt, handover run: 5 / 27 / 24; repeat: 8 / 22 / 26
- new prompt: 8 (+1 partial) / 20 / 27; repeat: 7 / 21 / 28

Controls (n = 150) — kept / declined / changed:
- old prompt, handover run: 136 / 12 / 2; repeat: 139 / 9 / 0
- new prompt: 141 / 9 / 0; repeat: 143 / 7 / 0

17 judged-wrong cases are wrong in all four runs. The suffix fix corrects Abrahams & Mauer
1999 in both new runs; Eckman 1981 stays wrong (its pick was the unsuffixed key).

## Pilot mechanics (`pilot_150.csv`)
Dry run offered 149; run: 149 extracted, 143 resolved, 5 target_pending, 1 provisional.
Rendered to `extracted_sandbox.csv` (`data/` untouched). Outcome changed on 18 of 129
same-original works — mostly the model change.

## Spend
gpt-6-luna for the whole day, all agents: 11.1M in / 2.7M out; flex mostly refused
(see open question 2). ≈$2.5 at twice flex price. OpenAlex ≈1,000 credits (dry-run estimate).

## Open questions
1. The declines: a redo that turns a settled resolved link into `target_pending` drops a
   row the old verdict shipped correctly 5 times in 6. Consider a redo rule that keeps the
   previous resolved verdict when the new one only declines.
2. `_openai_flex_refused()` checks `code == "resource_unavailable"`; OpenAI now sends
   `flex_unavailable`, so each refused flex call wastes one retry before falling back.
3. `author_year_candidate_keys()` still uses letter suffixes (a test pins `@smith2010b`).
4. `llm_cited_candidates` (265 rows) and `llm_fulltext` (71 rows) picks carry the same risk
   and were not measured.
5. clean_doi: see `clean_doi_impact.md` — prefer a comparison-only `doi_match_key()`.
