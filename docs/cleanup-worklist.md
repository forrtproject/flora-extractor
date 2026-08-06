# Cleanup worklist — completion record, 2026-08-06

The worklist collected during the html-comments fix round has been worked. This file
replaces it: one line per original item, with what happened. Items still open are in
the last two sections.

Outcomes are recorded per item as **done**, **skipped** (with the reason), or
**pending** (waiting on a maintainer decision).

## Dead code

| Item | Outcome |
| ---- | ------- |
| `shared/llm_client.py` — unused `from pathlib import Path` | **pending** — still imported at line 22 |
| `claims.py` — `record_supersession()`'s `actor` argument | **skipped**: the worklist's premise was false. `supersede.py` passes `actor`, so the argument has a caller |
| `backfill.py` — `_rows()` / `estimate()` unreachable from `main()` | **done** — deleted; `estimate_worklist` is tested directly |
| `filter/spec/holdout.json` plumbing | **pending maintainer decision** (#146-2: build the holdout set, or remove the plumbing). Every holdout reference is deliberately untouched until that call is made |
| `link_original.py` — `clear_pipeline_caches` sweeping the dead `match_type` / `multi` prefixes | **pending** — waits on the old cache dirs being gone |
| `link_original.py` — `flora_df` / `_FLORA_COLS` / `_flora_row` | **done** — deleted; no production caller ever passed `flora_df`, so every `flora_*` column was always blank |
| `link_original.py` — the journal-hint branch of citation scoring | **done** — deleted. `citation_context_match` therefore has no live writer; historical rows carry it, so it stays in `RESOLVED_LINK_METHODS` and in every vocabulary list |
| `link_original.py` — `_resolve_by_title_pattern`'s dead base-vs-None return shape | **pending** |
| `spec.py` — `MatchBlock.pyre_regex` | **skipped**: kept as the deliberate record it is. It is now documented in `docs/filter-engine.md` as well as `CONVENTIONS.md`, so a reader meets it in the spec-language section rather than only in the rule notes |
| `FLORA_READONLY` stragglers | **done** — two comment references in `requirements.txt` removed; no code reads it |

## Docs-vs-code mismatches

| Item | Outcome |
| ---- | ------- |
| `overlay.py:111-114` sizing comment implying a millions-of-rows worklist | **pending** |
| `cli-reference.md` — `export` writes `FILTERED_COLS` + `ENGINE_EXPORT_COLS` | **done** — it writes `ENGINE_EXPORTED_COLS`, the six `SCREEN_COLS` blank. Corrected in `cli-reference.md` and `filter-engine.md`, both now saying why blank columns are what lets Stage 3 accept an exported pile |
| `llm_client.py` — "Gemini (primary)" / "OpenAI (fallback)" section headers | **pending** — both headers are still there, and there is no primary or fallback |
| `release.py:21` — comment implying `RELEASE_INPUTS` order matters | **pending** |
| CLAUDE.md — "filtered.csv … has reached multiple GB" | **done** — the engine handoff is 1,614 rows (`data/filtered.csv.manifest.json`); the multi-GB file is the retired pre-engine one, still in DVC at 1.7 GB zipped. Swept the rest: CLAUDE.md said the snapshot was 400 GB (it is 725 GB), `setup.md` sized a live `filtered.csv` at ~4.3 GB, and `csv-schema.md` claimed `OUTCOME_LEGACY_VALUES` is empty forty lines after listing its nine members |
| `shared/schema.py` — eight value-set constants validating nothing | **done** — six wired into `tests/test_schema_roundtrip.py` against the samples in `misc/`; `OUTCOME_STATE_MARKERS` was already load-bearing inside `schema.py`; `VALIDATION_STATUS_VALUES` deleted (Stage 4 writes no CSV, and the `validation_status` strings this repo reads are the FLoRA entry sheet's, a different vocabulary) |
| `single_candidate_after_requery` described as "weakest, no semantic check" | **done** — still true, and both `csv-schema.md` and `schema.py` now say it is HELD-ONLY: parked by `_HELD_ONLY_METHODS`, restored only when nothing that can enumerate targets contradicted it |
| `osf-registration-protocol.json` — description is a long changelog | **pending** — 775 words, not the ~3,000 the worklist estimated, but still a history rather than a description |
| `misc/` sample CSVs | **done** — `sample_filtered.csv` regenerated to the 27-column `ENGINE_EXPORTED_COLS` contract with the `<model>: <quote> \|\| <model>: <quote>` evidence format; `sample_candidates.csv` regenerated (it was missing `ref_r` and was unparseable — 11–12 fields against a 9-field header). Both `xfail(strict=True)` markers removed |

## Refactoring opportunities

| Item | Outcome |
| ---- | ------- |
| Three Gemini call sites sharing a rotate-plus-retry skeleton | **done** — unified on one `_gemini_call` loop |
| `call_gemini(prompt, model=PDF_PARSE_MODEL)` default | **done** — the default is dropped; `call_gemini` requires a model |
| `_clean_study_number` / `_clean_study_numbers` | **pending** |
| `tiers.py` (1,166 lines) four-way split | **skipped**: 18 test sites monkeypatch `tiers` as a module. Re-exporting the split pieces back through `tiers` would keep imports working but break patch semantics, which is worse than the length |
| `checkpoint_decisions()` double-read | **not real** — measured: one fetch per run. The post-run incomplete count must be a fresh read, so the second call is doing different work, not repeating the first |
| `export.py` / `handoff.py` — `_write_csv` and `_write_csv_tmp` | **done** — unified on the atomic writer, `write_rows_tmp`, so `export_pile` publishes atomically too. `decisions()` now returns a 2-tuple, `decided` being derivable from the rest |
| `release.py` — `write_release()` re-deriving the release id | **done** — `write_release` takes the id its caller already computed |
| `overlay.py` — hex-string vs raw-digest hash style | **skipped, with a comment**: unifying would move hashes already persisted in release ids and overlay manifests. The comment now says why the two styles differ |
| `db/migrations/` — `engine_claim_batch` copied in 0001/0003/0004 | **pending** — the real fix is the migration runner (W7). `db/migrations/` is append-only and was not touched |
| `claims.py:_tier_of()` substring-scanning an error body | **done** — the tier is threaded through instead of guessed |
| `sizing.py` — `AUDIT_ROWS_PER_CLAIM = 2` | **done as documentation** — it is now stated as an upper bound: an expired claim writes one audit row, so the estimate runs slightly high on purpose |
| `link_original.py` — `_study_count_stated` using `.search` | **pending** |
| `link_original.py` — self-link guards duplicating `exclude_doi` | **skipped**: the twin lives in `shared/doi_verify.py` and behaves differently. A shared helper would have to serve both behaviours, which is not a dedup |
| `pdf_sources.py` — no up-front on-disk check in `acquire_pdf` | **done** — a designed up-front check replays a recorded winning-tier label from `pdfsrc_<key>.json` |
| `pdf_sources.py` — per-URL failure record | **done** — a per-URL gone-record (`retry_<key>.json`, 404/410 only, the same 14-day window, deleted on a later success) |
| Two belt-and-braces content-free-XML guards | **done** — the demotion in `run_for_doi` is removed; `get_openalex_fulltext` and `acquire_pdf` both refuse content-free results already |
| `backfill.py` — `--phase bulk` running Europe PMC only | **done** — a one-line notice when OpenAlex is excluded by default |
| `spec.py` — `POLICY_FILES` one-element tuple with a defensive sort | **skipped, kept**: the sort is insurance on a hash input. It costs nothing and a bundle hash that silently depends on iteration order would be expensive |

## Untested load-bearing modules

**done** — `shared/openalex_keys.py`, `shared/token_counter.py` and
`extract/backfill_authors.py` are now covered, +27 tests.

## Bigger open items — tracked elsewhere, do NOT fold into a cleanup pass

W1 re-route + re-hand-off; W4 `token_usage` unreadable-vs-empty; W6 dashboard refresh
(#115); W7 Postgres index + migration runner; Stage 3 atomic-replace output write;
`pool_reader` overlay-policy contradiction (maintainer must pick the contract first);
CI job for the suite; retiring the `feature/extract` deploy trigger; issue #183
(`llm_title_search` confirmation call); flora-validation #6/#7.
