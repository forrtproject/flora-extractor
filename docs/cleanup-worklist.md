# Cleanup worklist — 2026-08-06

Instructions for a dedicated clean-up pass. Collected during the html-comments fix
round (`fix/html-comments-round2`): everything below was noticed and deliberately NOT
fixed there, to keep that branch focused on correctness. Nothing here changes what the
pipeline concludes; treat items as independent, smallest first. Verify each against
the code before acting — some may have moved.

## Ground rules for the cleanup agent

- One concern per commit; run `python -m pytest tests/ -q` after each.
- Deleting dead code beats commenting it out; delete its tests with it.
- When a docstring and the code disagree, the code is the contract unless the item
  says otherwise — one exception is flagged (pool_reader), where the CONTRACT itself
  is undecided and a maintainer decision is needed first.
- Do not touch: cache-key construction, model constants, spec precedences, anything
  under `db/migrations/` (append-only), or the schema column list.

## Dead code

- `shared/llm_client.py:22` — `from pathlib import Path` unused.
- `filter/engine/claims.py` — `record_supersession()`'s `actor` argument: no caller
  passes it.
- `filter/engine/backfill.py` — `_rows()` and `estimate()` are unreachable from
  `main()`; only tests call them (`tests/test_engine_overlay.py`). Delete and test
  `estimate_worklist` directly.
- `filter/spec/holdout.json` plumbing — the file has never existed; referenced by
  `spec.py` (exclusion list), `diagnostics.py` (`not_constructed`), docs, and a test
  asserting its absence. Either build the holdout set (#146-2) or remove the plumbing.
- `extract/link_original.py` — `clear_pipeline_caches` still sweeps the `match_type`
  and `multi` cache prefixes; nothing writes either (labelled legacy in the docstring
  now). Remove once old cache dirs are gone.
- `extract/link_original.py` — `flora_df` / `_FLORA_COLS` / `_flora_row` are
  effectively dead: the only production caller never passes `flora_df`, so every
  `flora_*` output column is always blank.
- `extract/link_original.py` — the journal-hint branch of the citation-scoring path
  (`_fetch_journal_cached`, `_JOURNAL_TAIL_RE`, the +3.0/+1.5 weights) has zero
  observed firings in production (`citation_context_match` = 0 rows). Decide: keep as
  a documented feature or delete.
- `extract/link_original.py` — `_resolve_by_title_pattern` documents three return
  shapes but the caller only tests `.get("resolved")`; the base-vs-None distinction is
  dead.
- `filter/engine/spec.py` — `MatchBlock.pyre_regex` is loader-only: validated, never
  evaluated. It is a deliberate record; consider moving the information to
  `rule_ideas.md` and dropping the field.
- `FLORA_READONLY` — no longer read anywhere after the `/batch` removal; grep once
  this branch lands and delete any straggler mention.

## Docs-vs-code mismatches

- `filter/engine/overlay.py:111-114` — sizing comment still implies a pool-wide
  worklist runs to millions of rows (partially corrected; re-read after the backfill
  changes).
- `docs/cli-reference.md` — the `export` section says it writes "`FILTERED_COLS` +
  `ENGINE_EXPORT_COLS`"; it writes `ENGINE_EXPORTED_COLS` (i.e. including blank
  `SCREEN_COLS`), which is exactly why Stage 3 accepts an exported pile.
- `shared/llm_client.py` — section headers "Gemini (primary)" / "OpenAI (fallback)"
  predate the no-fallback rewrite; there is no primary or fallback.
- `filter/engine/release.py:21` — comment says `RELEASE_INPUTS` order matters;
  `_release_id()` sorts keys, so it does not.
- CLAUDE.md — "filtered.csv … has reached multiple GB" describes the retired
  pre-engine file; the engine handoff on disk is 1,614 rows. Sweep for other
  stale size claims.
- `shared/schema.py` — eight value-set constants validate nothing (from the
  2026-08-06 survey, W9). Either wire them into `validate_csv_columns()`-style checks
  or delete them.
- `docs/csv-schema.md` + `shared/schema.py` — `single_candidate_after_requery` is
  described as "weakest, no semantic check"; still true, but both places should note
  it is now held-only (LLM-confirmed in the common case).
- `filter/spec/osf-registration-protocol.json` — the `description` field is a
  ~3,000-word changelog; move the history to the PR record / rule_ideas.md and keep a
  description that describes.
- `misc/` sample CSVs — check they still match the 27-column contract after the
  screen-evidence format change (`<model>: <quote> || …`).

## Refactoring opportunities

- Three Gemini call sites (`call_gemini`, `call_gemini_with_pdf`,
  `call_gemini_with_images`) share an identical rotate-plus-retry skeleton — now at
  the "three uses" threshold for a `_gemini_call(payload, timeout)` helper. Touches
  the flex and token-recording paths: do it alone, with the llm tests open.
- `call_gemini(prompt, model=PDF_PARSE_MODEL)` — a text-JSON call defaulting to the
  PDF model; only reachable by mistake. Drop the default.
- `_clean_study_number` / `_clean_study_numbers` (llm_client) — two near-identical
  helpers where one would do.
- `filter/engine/tiers.py` (1,166 lines) — four concerns (estimating,
  claiming/running, HF uploading, decision read-back). Also `checkpoint_decisions()`
  re-fetches all claims and verdicts per call and `_batch()` triggers it twice per
  run: two full verdict reads where one would do.
- `filter/engine/export.py` / `handoff.py` — `_write_csv` and `_write_csv_tmp` are
  the same DictWriter loop; only the atomic temp-file publish differs, and
  `export_pile` writes non-atomically while the handoff does not. Unify on the atomic
  one. Also `UNCHECKED`/`_UNCHECKED` are two names for one sentinel across the two
  modules, and `handoff.decisions()` returns `decided` alongside `drop` when the
  second is derivable from the first.
- `filter/engine/release.py` — `write_release()` re-derives the release id its caller
  already computed; two computations that must agree.
- `filter/engine/overlay.py` — the overlay hash folds a hex-string sub-hash where
  `bundle_hash` folds raw digest bytes; two hashers, two styles.
- `db/migrations/` — `engine_claim_batch` is now copied whole in 0001, 0003 and 0004
  (plpgsql forces it) and nothing tests the copies agree. The real fix is W7's
  migration runner + tracking table.
- `filter/engine/claims.py:_tier_of()` — guesses the tier by substring-scanning an
  error body.
- `filter/engine/sizing.py` — `AUDIT_ROWS_PER_CLAIM = 2` assumes every claim
  releases; an expired claim writes one audit row, so the estimate runs slightly high.
- `extract/link_original.py` — `_study_count_stated` checks only the FIRST match of
  each pattern (`.search`); "Replications of 2019 studies … we replicate three
  findings" slips through because the year match short-circuits. Use `finditer`.
- `extract/link_original.py` — the self-link guards in `_search_title_for_original`
  duplicate `identify_targets_with_llm`'s `exclude_doi` logic; shared helper.
- `shared/pdf_sources.py` — `acquire_pdf` never checks up front whether the PDF is
  already on disk; the short-circuit happens accidentally inside the winning tier's
  `download_pdf`. A designed up-front check would be clearer.
- `shared/pdf_sources.py` — `download_pdf` caches only successes; a permanently dead
  URL is re-fetched once per 14-day retry window. A per-URL failure record would be
  tighter than the per-tier one.
- `shared/pdf_sources.py` / `extract/link_original.py` — two belt-and-braces
  content-free-XML guards remain now that `get_openalex_fulltext` and `acquire_pdf`
  both refuse content-free results; the demotion in `run_for_doi` can no longer fire
  from that path. Keep one, comment it as legacy-cache defence, or remove after the
  old shells are purged.
- `filter/engine/backfill.py` — `--phase bulk` with no `--source` now runs Europe PMC
  only; a script that assumed the old two-source bulk pass silently does half the
  work (the dry-run table does show it). Consider a one-line notice when OpenAlex is
  excluded by default.
- `filter/engine/spec.py` — `POLICY_FILES` is a one-element tuple with a defensive
  sort; fine, just noise if it never grows.

## Untested load-bearing modules (carried from the state page §7)

- `shared/openalex_keys.py` (rotation for a metered, budgeted API),
  `shared/token_counter.py`, `extract/backfill_authors.py` (a write path over
  production data).

## Bigger open items — tracked elsewhere, do NOT fold into the cleanup pass

- W1 re-route + re-hand-off; W4 `token_usage` unreadable-vs-empty; W6 dashboard
  refresh (#115); W7 Postgres index + migration runner; Stage 3 atomic-replace output
  write; `pool_reader` overlay-policy contradiction (maintainer must pick the
  contract first); CI job for the suite; retiring the `feature/extract` deploy
  trigger; issue #183 (`llm_title_search` confirmation call); flora-validation #6/#7.
