#!/usr/bin/env bash
# Close the issues resolved as of 2026-07-22, each with the reason it is resolved.
#
# Requires an authenticated GitHub CLI:
#     gh auth login
#
# Review before running — this closes 10 issues and 1 PR.
set -euo pipefail
R=forrtproject/flora-extractor

# ── Resolved by the feature/issues-68-69 branch ──────────────────────────────

gh issue close 69 --repo $R --comment "Done.

\`oa_work_id_r\` and \`oa_work_id_o\` are now part of \`EXTRACTED_COLS\` (\`shared/schema.py\`), storing the **bare** OpenAlex work ID (\`W2884670852\`), not the URL form.

**Populated automatically going forward.** \`run_extract._fill_work_ids()\` runs inside \`_append_row\`, the single choke point every written row passes through — so single-original, multi-original and \`--extracted-test\` are all covered with no flag. It runs *after* \`_verify_row\`, so when DOI verification corrects \`doi_o\` the o-side id describes the DOI that actually got written rather than the one the LLM first proposed.

Cost is close to zero on the r-side: Stage 1 already carries \`openalex_id_r\` as \`https://openalex.org/W…\`, so that is a string strip. Only the o-side needs a lookup, via the existing cached \`fetch_openalex_by_doi\`.

**Backfill of the existing rows — done.** New tool \`tools/backfill_oa_work_ids.py\` (dry-run by default, \`--apply\` to write, takes the same \`csv_lock\` as the streaming appender). Result on \`data/extracted.csv\`:

| column | filled | note |
| --- | --- | --- |
| \`oa_work_id_r\` | 1568 / 1568 | all from \`openalex_id_r\`, no API calls |
| \`oa_work_id_o\` | 1514 / 1568 | of the 54 blanks, 38 have no \`doi_o\` at all; 16 DOIs are not indexed in OpenAlex |

All values validated against \`^W\\d+$\`; schema column order preserved.

**Also pushed to Supabase**: \`csv_to_db._build_metadata_row\` now sends both columns.

Tests: \`TestBareWorkId\` in \`tests/test_utils.py\`, \`TestFillWorkIds\` in \`tests/test_extract.py\` (including the corrected-DOI ordering case)."

gh issue close 68 --repo $R --comment "Fixed, though the diagnosis changed twice — the final answer is not the obvious one.

## The dashboard was measuring the wrong thing

\`cache/openalex/\` holds **45,866 cached result pages** but only **853 cursor checkpoints**. Those checkpoints account for 1.31M fetched records; the page files hold up to ~9.2M, and sampling them shows plenty of 2012–2026 publication years. \`candidates.csv\` has **1,077,237 rows for 2011–2021** on a smooth year-on-year curve with no truncation cliff.

So the searches did run. Their checkpoints were pruned — \`cache/\` is gitignored and prunable, and a cleared checkpoint leaves the rows in \`candidates.csv\` with nothing left to attribute them to. \`phrase_yield()\` reconstructs everything from those checkpoints, so it was reporting 1.31M when far more had been fetched. **That is the reason the numbers did not add up.**

The dashboard now says so: coverage figures are labelled as accounting for surviving checkpoints only, and the per-year badge reads **no checkpoint** (gray) rather than implying the work was never done. \`candidates.csv\` is the authority on what was fetched.

## What is genuinely broken, and is now fixed

- **Empty jobs never checkpointed as complete.** \`fetch_phrase\`/\`fetch_concept\` broke out of the loop on an empty first page *before* writing \`completed=true\`, so a genuinely-zero job was re-requested on every subsequent run forever, and was indistinguishable from a truncated one. 21 \`registered replication report\` year-jobs were stuck this way.
- **No way to detect under-fetching.** Cursors now record OpenAlex's own \`meta.count\` as \`api_total\`, and \`phrase_yield()\` returns \`expected\`/\`incomplete\`/\`years_missing\`, with \`expected_partial\` so a pre-change checkpoint cannot read as 100% coverage.
- **The endpoint served a stale \`stats.json\`** snapshot predating these fields; it is now detected and labelled.

## Two real gaps found along the way

1. The \`replication of\` job for **2011** stopped mid-pagination at **10,000 of 33,914** and never resumed. \`_get_page\` raises \`StopIteration\` on \`Retry-After > 600\` (OpenAlex quota exhausted) and saves the cursor — correct, but nothing resumes it.
2. \`candidates.csv\` was last written **2026-07-12** while cursors ran on **07-14**, so some fetched pages were never merged. \`python -m search.run_search --harvest-only\` merges them with no new API calls.

## Separately: three of the phrases are not phrases

OpenAlex strips stopwords, so a quoted phrase whose only content word is a single term collapses to that one-word query. Verified by reversing word order — identical count means no phrase match:

- \`\"replication of\"\` = \`\"of replication\"\` = \`\"replication\"\` = **1,299,397**
- \`\"direct replication\"\` 1,809 vs reversed 115 → genuine phrase match
- \`\"we replicated\"\` 14,023 vs reversed 9,168 → genuine phrase match

The degenerate ones are \`replication of\`, \`reproducibility of\`, \`replicability of\` — also the top three by yield. The dashboard flags them **not a phrase**. The stopword set is deliberately minimal: only \`of\` is confirmed dropped, and \`we\`/\`not\`/\`did\`/\`could\` were each measured *not* to be.

Written up in \`docs/limitations.md\` (e) and (f). Tests: \`TestCursorCompleteness\`, \`TestPhraseYieldCoverage\` in \`tests/test_search.py\`."

gh issue close 45 --repo $R --comment "Both halves are now fixed.

**\`call_openai\` retry** (done earlier): 3 attempts with exponential backoff, per the \`api_error\` contract in CLAUDE.md.

**\`run_filter\` semantics** (this change): when \`_llm_classify\` returns \`None\` — every model failed after its own retries — the row is no longer written as \`needs_review\` and its key is no longer appended to the filtered index. Both were bugs: the first made a transient outage indistinguishable from genuine uncertainty, the second retired the row permanently so resume never revisited it.

The row is now left unwritten *and* unindexed, so the next run reprocesses it from \`candidates.csv\`. Writing it and retrying later would duplicate it, since the index is the only dedup mechanism. A run-level warning reports the deferred count.

Test: \`test_total_llm_failure_defers_the_row_for_retry\` in \`tests/test_filter_streaming.py\` — asserts no \`needs_review\` leaks into \`filtered.csv\`, the key stays out of the index, and a second run with a working LLM picks the row up without duplicating it.

Note for anyone reading the diff: four existing tests stubbed \`_llm_classify\` to return \`None\` purely as a no-op. That return value now means 'total failure', so they were switched to a successful stub — their subject (key ordering and resume) is unchanged."

# ── Verified already resolved in main ────────────────────────────────────────

gh issue close 54 --repo $R --comment "Resolved — \`extract/mix_for_validation.py\` was deleted. Direct sampling from \`extracted.csv\` by \`csv_to_db.py\` is the intended design, so the orphan producer (and with it the \`pending\`/\`api_error\` leak in its 'other' pool) is gone rather than wired in. Verified absent from \`main\`."

gh issue close 53 --repo $R --comment "Resolved. \`_row_keys()\` in \`search/run_search.py\` now adds the \`title:\` key **only** when a row has no DOI, OpenAlex id or URL — so two distinct works sharing a title (Reply/Commentary pairs, RRR stubs, identically-titled corrections) are both kept.

Note: run \`python -m search.run_search --rebuild-index\` to purge stale title keys written by the old behaviour."

gh issue close 51 --repo $R --comment "Resolved via the issue's own fallback recommendation: \`run_extract\` now caps \`link_confidence\` at \`medium\` for \`single_candidate_after_requery\` instead of letting it through at \`high\`, so these rows sort to the front for validation rather than propagating unchecked at top confidence.

The internal \`resolution_score = 1.0\` in \`link_original.py\` is unchanged — it is the rule-path's own score, not the exported confidence. Adding a semantic corroboration step before accepting a lone candidate remains worthwhile; that belongs with the linking-precision work in #52."

gh issue close 49 --repo $R --comment "Resolved. \`promote_rows()\` now takes \`shared.utils.csv_lock\` around its read-modify-write, and \`run_extract\`'s per-row appender takes the same lock, so a promote can no longer clobber rows appended while it was working."

gh issue close 47 --repo $R --comment "Resolved. \`search/semantic_scholar_search.py\` now imports \`SEARCH_PHRASES\` directly from \`search/openalex_search.py\` rather than keeping its own 10-phrase copy, so both sources query the same 37 phrases including the failure-signal ones. The directional source bias is gone by construction — the lists cannot drift apart again.

Guarded by \`test_s2_phrases_match_openalex_and_include_failure_signals\` in \`tests/test_search.py\`."

gh issue close 46 --repo $R --comment "Resolved by taking the 'remove the claims' option: the \`run_search\` docstring no longer advertises I4R / Replication Network harvesting (it now says the scrapers exist but are not wired in), and \`SOURCE_VALUES\` in \`shared/schema.py\` is down to what is actually produced — \`openalex\`, \`openalex_concept\`, \`semantic_scholar\`, \`backfill_old_pipeline\`.

The scrapers in \`search/external_lists.py\` are kept. Wiring them in — and fixing the page-1-only pagination bug — is real recall still on the table; worth a fresh issue if it gets picked up."

gh issue close 6 --repo $R --comment "Resolved — \`filter/llm_filter.py\` exists with \`classify_with_llm\`, caching, rate limiting and env-configured models, and is called from \`run_filter\` only for \`needs_review\` rows. The retry/failure semantics this issue specified were finished in #45."

# ── PRs ──────────────────────────────────────────────────────────────────────

gh pr close 40 --repo $R --comment "Closing without merging — \`main\` is 102 commits ahead of \`dev\`, and the single commit here (\`0226620\`, the #39 merge) is already contained in \`main\`. \`dev\` is stale; feature branches should be based on \`origin/main\`."
