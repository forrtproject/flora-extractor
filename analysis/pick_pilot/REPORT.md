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

## v2 pilot — blind pick check wired in (2026-09-23, sandbox)

Same 150 works, same truth coding. v2 = v1 (gpt-6-luna, sibling-rule prompt, `_2` keys)
+ af85ea6 (full-text rung sends the whole document; a carried original keeps the
full-text call's settled outcome) + the blind reference-pick check (ladder 28,
`check_reference_pick`, `deepseek/deepseek-v4.1-flash` @ low, the measured
`analysis/contrastive_confirm` blind prompt verbatim). Generation 474e80e4c32a6dfa →
**765e053cd24611e5** (declared equivalent, chain flattened).

Run: `--mode validation --redo <150 ids> --only <150 ids>`. The `--redo-status
llm_references` form named only 129 of these, because it reads the stored v1
validation-mode verdicts and 20 of them had not ended at `llm_references` (title search,
resting `target_pending`). 150 extracted, 142 resolved, 8 target_pending; rendered to
`extracted_sandbox_v2.csv` (`data/` untouched, no `retired_pairs.csv` — the manifest is
live + production path only). Tables: `pilot_150_v2.csv`, `truth_scored_v2.csv`,
`truth_scored_old_v1_v2.csv` (`build_pilot_table.py --sandbox … --out …`,
`score_truth.py <pilot> <out>`).

| Score against truth (works) | live | v1 | v2 |
|---|--:|--:|--:|
| right | 134 | 133 | 132 |
| partial | 2 | 4 | 5 |
| wrong original shipped | 12 | 5 | **4** |
| shipped a row for a non-replication | 2 | 2 | **1** |
| nothing shipped (`target_pending`) | 0 | 6 | 8 |
| works with a `cannot_be_determined` row | 20 | 22 | **19** |

old → v1 → v2 paths (only non-constant ones): right→none→none 5; right→wrong→none 1
(Giddens, below); wrong→none→none 1; nar→nar→none 1 (Bailey); right→right→partial 1;
wrong→right→partial 1; wrong→partial→right 1. v1→v2 moved 5 works: 2 to `none` (both
flag catches), 1 partial→right, 2 right→partial. The partials are multi-original works
where the full-text/title-search route added or dropped a secondary original, not a
wrong single pick. The partial/right edge is approximate (title-prefix matching).

**Flags: 5 of the 120 accepted `llm_references` picks (4%), 115 confirmed, 0 api_error.**
- Real, 2: W2611033029 (v1 shipped the sibling `@giddens2009`; checker named
  `@giddens2007_2`, the judges' original; no document → declined, `target_pending`) and
  W2509481270 (judges: not a replication; declined instead of shipping a row).
- False, 3, none of them costly: W2049839209 (`@lorentzen2007` correct; the full text
  re-found it → still right), W4389037127 and W4385969725 (multi-original works where
  the pick was ONE right original; the full text named both → right, 2 rows each).
- Not caught: all 4 still-wrong single picks were `pick_check: confirmed`, as the
  offline replay predicted (≈15 of 25 repeats get past every checker).
- Found by this run: on a flagged work whose full text names several originals, the
  per-target rows lost the flag note (their evidence comes from each target). Fixed
  after the run (`pick_check` output key, appended in `_per_target_rows`); the two
  stored v2 verdicts affected lack the note.

Review fixes after the run (same generation): a flag now also voids an earlier
acceptance of the SAME record (a carried abstract link and its certain target, a
withheld rule pick — the latter compared against the flagged pick only), and a check
with no usable answer ends the row `pick_check_failed` → `api_error` instead of letting
the pick settle unchecked. None of these reaches a pilot work: no check failed, and none
of the 5 flagged works had an abstract-rung acceptance or a withheld rule pick (run log),
so the v2 numbers stand without a re-run.

cbd: 19 works vs 22 (v1) and 20 (live). This is mostly af85ea6; the check itself
touches outcome only through the 5 flagged works.

**Spend:** gpt-6-luna 361k in / 59k out (flex mostly refused, so standard tier),
DeepSeek 255k in / 83k out — ≈ $0.14 in all. OpenAlex ≈ 1,050 credits (dry-run
estimate).

**Recommendation.** v2 is at least as good as v1 on every row of the table, and the check
cost no correct link on this sample. Go ahead with the live redo of the `llm_references`
population under 765e053cd24611e5. Scaled, that is ≈ $1.5 plus OpenAlex. The
check is a small lever: 1 wrong single pick caught out of 5 remaining, at 4% flags.
Most of the gain over live is still the model and the prompt. Open question 1 above (a
redo that only declines drops a correct live row; 6 of 150 here) is unchanged and
matters more than the check does.
