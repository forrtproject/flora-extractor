#!/usr/bin/env bash
# Comment on the issues/PRs that stay OPEN, recording where they actually stand.
# Nothing here closes anything.
#
# Requires: gh auth login
set -euo pipefail
R=forrtproject/flora-extractor

gh issue comment 52 --repo $R --body "One agenda item is now answered.

**'Test whether OpenAlex \`.search\` actually honors quoted phrases.'** Measured 2026-07-22 against the live API (\`publication_year:1990-2026\`), by checking whether reversing word order changes the count:

| query | count | reversed | verdict |
| --- | --- | --- | --- |
| \`\"replication of\"\` | 1,299,397 | 1,299,397 | **no phrase match** |
| \`\"direct replication\"\` | 1,809 | 115 | phrase match |
| \`\"we replicated\"\` | 14,023 | 9,168 | phrase match |
| \`\"could not reproduce\"\` | 6,381 | 1,133 | phrase match |
| \`\"did not replicate\"\` | 2,409 | 414 | phrase match |

So quoting **does** work — but stopwords are stripped first, and a phrase whose only content word is a single term degenerates to that one-word query. \`\"replication of\"\` is identical to bare \`\"replication\"\`.

This hits three phrases: \`replication of\`, \`reproducibility of\`, \`replicability of\` — which are the **top three by yield, 78% of everything fetched**. The phrase-precision assumption is wrong for exactly the phrases that dominate the corpus, so any Stage 1 precision or per-phrase recall number in the report needs to treat those three as broad single-word queries. Details in #68 and \`docs/limitations.md\` §(e).

Two caveats for the rest of the agenda: OpenAlex now meters requests against a daily USD budget (a few hundred metadata calls exhausted the free tier mid-measurement), so budget for that when running the sensitivity and gold-sample work. And note the corpus itself is only ~25% fetched for the big phrases (#68) — recall estimates computed on today's \`candidates.csv\` will be measuring an incomplete crawl, not the phrase gate."

gh issue comment 48 --repo $R --body "Half of this is fixed; the other half is still an open decision.

**Done — staleness no longer silently empties the source.** \`check_spec_freshness()\` in \`search/engine/runner.py\` now warns instead of raising, so a >60-day-old spec no longer turns into \`status=failed\` → empty DataFrame → zero candidates behind a buried log line.

**Still open — additive vs. replacement.** In \`search/run_search.py\`, \`if is_engine_enabled():\` still sits in the branch whose \`else\` runs the OpenAlex phrase search, so \`FLORA_USE_ENGINE=1\` *replaces* phrase search rather than adding to it, and the engine's OpenAlex adapter still appends \`has_abstract:true\` (dropping abstract-less recall). That needs a deliberate call on what the engine is *for* before it can be coded either way — leaving this open on that question.

Context worth folding in: #68 found Stage 1 has only fetched ~25% of what the phrase queries actually match (years 2012–2026 were never run). Any measurement comparing engine yield against phrase yield right now would be comparing against an incomplete crawl."

gh issue comment 65 --repo $R --body "Still reproducible on \`main\`: \`search/fetch_abstracts.py\` lines ~426/429 still do a whole-file \`pd.read_csv(CANDIDATES_PATH, ...)\`.

PR #67 fixes this and is **not merged** — it has 2 commits not in \`main\` (it stacks on #66). This issue should stay open until #67 lands."

gh issue comment 43 --repo $R --body "Still open — unchanged on \`main\`, and it needs measurement rather than a code change.

Confirming both mechanics from the report still hold: the tie-breaker \`min(raw_len // 5, 1000)\` is inert because every parser truncates \`raw_text\` at 5000 chars, and \`refs × 300\` still lets MarkItDown's regex-derived count (capped at 60) outweigh a structured GROBID parse.

Blocked on the same thing as #44 and #52: a small gold set of PDFs where the best parse is known. Without it, any reweighting is swapping one unvalidated constant for another."

gh issue comment 44 --repo $R --body "Still open. The targeted readmission rule — re-open rows where an exclusion pattern fired **and** a replication phrase **and** an author-year citation were all present — is the tractable half and does not need a labeled sample; the false-rejection *rate* does.

Worth noting before anyone estimates that rate: #68 found Stage 1 has only fetched ~25% of what the phrase queries match (2012–2026 was never searched). The ~2.17M no-phrase bucket is drawn from an incomplete crawl, so a false-rejection rate measured today would not generalise to the finished corpus. Recommend completing the Stage 1 backfill first."

gh issue comment 50 --repo $R --body "Still open — unchanged, and correctly so: (a) the slot-name mismatch between \`csv_to_db._VALIDATOR_SLOTS\` (\`human_1\`/\`human_2\`/\`llm\`) and \`supabase_client._SLOT_PREFIX\` (\`validator_1\`/\`validator_2\`/\`llm_validator\`) is a live data contract with the external validation repo. Picking a vocabulary unilaterally would break whichever side did not change.

(b)–(d) (missing \`validator_id\`, no Cohen's κ, verification-vs-blind-coding framing) likewise need the other repo in the room. Keeping this as the coordination tracker."

gh pr comment 66 --repo $R --body "Still unmerged — 1 commit not in \`main\`. The transient-vs-definitive fix here is not in \`main\` by any other route, so this should be merged rather than closed. #67 stacks on it and must merge after."

gh pr comment 67 --repo $R --body "Still unmerged — 2 commits not in \`main\` (including #66's, which it stacks on). \`main\` still has the whole-file \`pd.read_csv\` this PR removes, so the OOM in #65 is still live. Should be merged, not closed."

gh pr comment 42 --repo $R --body "Still unmerged — 1 commit not in \`main\`. The DVC/R2 setup is not in \`main\` by any other route, so this is a real open decision (adopt DVC or not), not completed work that can be closed."
