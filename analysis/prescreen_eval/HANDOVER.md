# Handover — issue #130, cheap Stage 3 pre-screen

Written 2026-08-03. Everything below is state you cannot reconstruct from the code and
`REPORT.md` alone. Read `REPORT.md` first for the evidence; this file is what to do next
and what will bite you.

## Where the work lives

- Worktree: `.claude/worktrees/issue-130-prescreen`, branch `worktree-issue-130-prescreen`.
- **PR #135 is MERGED** into main (squash commit `859560d`), containing everything up to
  `97e4428`.
- **Three commits are NOT in main and need a second PR**:
  - `190c093` pre-screen on by default + `PRESCREEN_MODE=shadow` + the correlated-failure finding
  - `2ee432b` corrected reasoning-model cost figure
  - `3ea6b4e` what the second voter actually buys

Open that PR first — the work is finished and tested, it is only unmerged.

## Read this before touching the default

`PRESCREEN_ENABLED` defaults to **1** (on, discarding). That was a deliberate user
decision on 2026-08-03, on the stated grounds that *"#129 will be the only path"* into
Stage 3 — i.e. the snapshot scanner would make the population too large to screen at full
price.

**#129 is currently reverted on main.** It merged as #134 (`7da4581`) and was reverted by
#138 (`1a50fa0`) about three minutes after PR #135 merged. `search/run_search.py` no
longer has the scanner.

So the justification for on-by-default is not currently in the codebase. This is not a
decision to reverse unilaterally — the user may well be planning to re-land #129 — but
**confirm it with them before the tier discards anything in a production run.** Without
#129 the whole tier nets ~$30 per corpus pass, which nobody thinks is worth a terminal
discard mechanism. With #129 it is ~$2,100.

If it needs turning off: `PRESCREEN_ENABLED=0`, or better `PRESCREEN_MODE=shadow`, which
records every verdict and acts on none.

## What the tier is, in one paragraph

Two cheap OpenRouter models are asked one question about title+abstract. The gate
DISCARDS only when both answer "no"; one keep, an unrecognised label, an unreadable
reply or a provider failure all pass the row to the validated screen unchanged. Before
any model runs, three bypasses send the row straight to the screen: an explicit
replication phrase (`hard_signal()`), a curated source, or an abstract under 200 chars.
Discards get `link_method = prescreen_discard`, land in `data/prescreen_discard.csv`,
never reach `csv_to_db`, and are reopened only by `--rescreen`.

Shipped config: prompt `p7` (`_PRESCREEN_PROMPT`), voters
`qwen/qwen3-30b-a3b-instruct-2507` + `mistralai/mistral-small-24b-instruct-2501`.
Measured: 87% of screen-confirmed negatives discarded, 64% net of the override,
0/567 gold positives lost (95% CI 0.00–0.67%).

## The three findings that should shape any further work

1. **The question must be answerable with "no".** "Could this paper be re-testing an
   earlier study?" is unanswerable under partial information and discarded 1–5%. Asking
   whether the text *suggests a deliberate check* discards 89–97%. This was the whole
   unlock; do not "simplify" the prompt back.
2. **The voters are not independent.** They fail together 37.8× more often than chance;
   when one misses a gold positive the other misses it 80% of the time. The AND gate is
   not the safety mechanism it looks like.
3. **The regex override is load-bearing.** It caught all four joint misses. One model
   plus the regex loses 1 of 567 against the pair's 0. The second model is insurance
   against provider drift, not the primary defence.

## Outstanding work, in priority order

### 1. Test and expand the override (started, NOT finished)

This is the highest-value remaining task and it is where I was interrupted. Rationale:
the override is load-bearing (finding 3) and was hand-written *after looking at the
misses it needed to catch*, so its measured perfection is partly circular.

The plan, which nothing has been written for yet:

- Build `override_positives.json`: every FLoRA replication/reproduction paper with a
  title AND abstract, from `data/all_replications.csv`, `data/flora.csv`,
  `data/flora_entry_sheet.csv`, `data/reproductions.csv` (cp1252), deduped on
  `clean_doi`. **Not** restricted to the Stage-3 population — the point is FLoRA-wide
  recall, so this should be thousands of papers, not the 567 already used.
- Build `override_negatives.json`: non-replications that DO reach Stage 3
  (`filter_status != false_positive` in `filtered.csv`), from `not_a_replication.csv`
  (`link_method == not_a_replication`) and the `false_positive` DOIs in
  `all_replications.csv`. Up to ~5,000. Tag each with its bucket.
- Backfill abstracts and build the negatives in **one** streaming pass over
  `filtered.csv` (4.9 GB, ~6 min, `csv.field_size_limit`, never load it into memory).
- Measure, per individual pattern in `_SIGNAL_PATTERNS`: hit rate on positives (recall)
  and on negatives (the cost — each hit is one needless $0.0018 screen call).
- Propose additions. **Derive them on one half of the positives and report only the
  held-out half**, or the numbers are meaningless. `README.md` explains the split rule.

Watch for: a pattern with high positive recall AND high negative hit rate is not
automatically bad — a needless override costs $0.0018, a missed one costs a paper. Bias
toward inclusion and say what the false-positive rate costs in dollars.

Sources worth mining that are NOT the current eval set, per codex's review: reproduction
and re-analysis vocabulary (which differs sharply from replication vocabulary),
non-English abstracts, and phrases appearing late in abstracts rather than at the start.

### 2. Run the curated-negative bucket under `p7`

`cases_live_goldneg_curated.json` (400 rows that genuinely reach Stage 3) has only been
run under `p1` and `p4`. The whole 64% benefit figure rests on one bucket of 184 rows.
One command per model, ~10 minutes:

```bash
set -a; source ~/.claude/api_keys.env; set +a
.venv/bin/python eval_prescreen.py qwen/qwen3-30b-a3b-instruct-2507 prompt_p7.txt \
    cases_live_goldneg_curated.json --workers=3
```

### 3. The shadow run — the only thing that settles this

`PRESCREEN_MODE=shadow` over fresh rows, then count how often the tier would have
discarded a row the validated screen went on to keep. Everything measured so far uses
gold positives that are the *easy*, canonical FLoRA entries, and negatives labelled by
the very screen under test. Codex's strongest point: also human-review a random sample of
rows where the pre-screen AND the screen both say discard — otherwise you only learn that
two correlated LLM systems agree.

### 4. Re-check `inclusionai/ling-2.6-flash`

Cheapest model in the field ($0.0055/1k, 3× cheaper than what ships) and never
evaluated: it has a single OpenRouter endpoint (Novita) that returned 429 to every call
on 2026-08-02, under every routing mode, with credit on the account. If it ever answers,
it is worth a full run — one env var to swap in, then re-measure.

## Gotchas that cost me time

- **Use the venv**: `/Users/lukaswallrich/Documents/Coding/flora-extractor/.venv/bin/python`.
  System python3 lacks `filelock` etc., and `shared/prescreen.py` imports the world.
- **`data/` only exists in the main checkout**, not in the worktree. Eval scripts use
  absolute paths to it. The 5 failing tests in `tests/test_analysis_overlap.py` and
  `tests/test_apa_resolver.py` fail for this reason on main too — they are pre-existing,
  not yours.
- **API keys** are in `~/.claude/api_keys.env`, not `.env`. `set -a; source …; set +a`.
- **OpenRouter credits**: ~$9 left of $35 at handover. The whole eval so far cost well
  under $1; a depleted balance returns **HTTP 402**, which is easy to misread as a model
  problem.
- **Keep `--workers` at 3–4.** Cheapest-provider routing pins one upstream provider that
  429s aggressively above that. High concurrency across several models at once is what
  stalled several runs.
- **Reasoning models are a trap here.** They bill thinking as output: qwen3.5-9b emits
  1,832 output tokens per call ($0.336/1k, 11× the shipped pair) against 9 for the models
  that ship. Judge candidates on measured output tokens, never the advertised input rate.
  Relatedly, `max_tokens=200` truncates them mid-thought and returns empty content —
  `eval_prescreen.py` now detects that and retries at 3,000, but if you see "schema
  errors" from a new model, check `finish_reason` before blaming the model.
- **Never select a prompt or model on the full eval set** and then report that number.
  These rows are derivation data; `score_prescreen.py --split=dev|test` exists for this.
- **A high discard rate on negatives means nothing on its own.** llama-3.2-3b discards
  94% of negatives and 39% of gold positives. Always run the positive buckets before
  believing a model is good.

## File map

| file | what |
| --- | --- |
| `REPORT.md` | the findings, with all the numbers |
| `README.md` | how to run the harness, the population, the split rule |
| `CASESETS.md` | how the four buckets were built and their caveats |
| `cases_live_*.json` | the eval population (rows that actually reach Stage 3) |
| `pre_<prompt>_<model>_<set>.json` | one voter's answers; resumable, delete to re-run |
| `eval_prescreen.py` | runner: `<model> <prompt.txt> <caseset.json…> [--workers=N] [--chars=N]` |
| `score_prescreen.py` | AND-gate scoring, Wilson intervals, bypass toggles |
| `build_casesets.py`, `enrich_casesets.py`, `rebuild_curated_neg.py` | corpus rebuilds |
| `prompt_p1..p8.txt` | p1/p3 minimal (useless), p4 first working, **p7 shipped**, p5/p6/p8 alternatives |

Production code: `shared/prescreen.py`, `_PRESCREEN_PROMPT` in `shared/prompts.py`,
`PRESCREEN_*` in `shared/config.py`, the call site in `extract/run_extract._process_row`,
`_prescreen_row`, the quarantine rule in `extract/sanity_check.py`, and
`tests/test_prescreen.py` (26 tests).
