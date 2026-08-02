# Handover — issue #130, cheap Stage 3 pre-screen

Written 2026-08-03. Everything below is state you cannot reconstruct from the code and
`REPORT.md` alone. Read `REPORT.md` first for the evidence; this file is what to do next
and what will bite you.

## Where the work lives

- Worktree: `.claude/worktrees/issue-130-prescreen`.
- **PR #135 is MERGED** into main (squash commit `859560d`), containing everything up to
  `97e4428`.
- **PR #139** carries everything after it, on branch `issue-130-prescreen-followup`
  (cherry-picked onto `origin/main`; the old `worktree-issue-130-prescreen` branch is
  superseded): on-by-default + shadow mode + the correlated-failure finding, the corrected
  reasoning-model cost figure, what the second voter buys, the override measured on 7,505
  papers and widened by 16 patterns, the last negative bucket, `ling-2.6-flash`, and this
  handover. Full suite green (974 passed, 25 skipped). Awaiting review.

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
#129 the whole tier nets somewhere between $3 and $30 per corpus pass (the two negative
buckets disagree by six-fold; see `REPORT.md`), which nobody thinks is worth a terminal
discard mechanism. With #129 the same rates scale to hundreds or low thousands.

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
Measured: 87% of screen-confirmed negatives discarded, **60%** net of the widened
override (64% before it), 11% of the curated-negative bucket, 0/567 gold positives lost
(95% CI 0.00–0.67%).

## The four findings that should shape any further work

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
4. **The override is also where the tier's benefit goes.** It fires on 83% of the
   curated-negative bucket and 36% of the screen-confirmed one, so a widened override
   directly buys safety with saving. Every pattern change needs both rates measured.

## Outstanding work

**One thing is genuinely open: the shadow run (§3).** Everything else on this list is
done, and is kept below because the findings, not the tasks, are what the next person
needs. The residue of §1 is the best follow-on if the shadow run is blocked.

### 1. ~~Test and expand the override~~ — DONE 2026-08-03, with a residue

Built and measured on 7,505 FLoRA papers and 1,333 live Stage-3 negatives
(`build_override_sets.py`, `eval_override.py`, `OVERRIDE_EVAL.md`; no LLM calls, all
rerunnable). The circularity worry was justified: the override was firing on **79.9%** of
positives, not the ~100% its 567-row score implied.

Sixteen patterns derived on the dev half of the misses and reported on a held-out half
are now **in `shared/prescreen.py`**: recall 79.9% → 94.7%, negatives 30.6% → 36.1%, for
~$7 a pass. Tier B (`replication of <study-like object>`, +1.1 points for $6) was left
out as the marginal call; tier C (bare `replication of`, +4 points for **$44** and half
the screen-confirmed negatives) was rejected — at that firing rate the regex disables the
tier.

What is left, in descending value:

- **The residue is still mostly vocabulary.** After tier A, ~88% of the remaining dev-half
  misses still contain a replication-family word. Another mining round would pay.
- **Reconsider tier B** if the shadow run shows the tier is saving more than expected.
- **The dead patterns.** #2, #13, #14, #15, #16 contribute zero unique matches. Harmless,
  but `re-analysis of the original/published` matching 1 paper in 7,505 says the
  reproduction vocabulary in the override was imagined rather than observed. Rewrite it
  from data if reproductions matter.
- **43 positives contain no `replicat*`/`reproduc*`/`re-analy*` at all.** No regex reaches
  them. This is the standing argument for the second voter.

Closed avenues, so nobody spends a day on them: **non-English vocabulary is a dead end**
(4 hits in 7,505 positives, 0 among the misses — `filtered.csv` abstracts are English),
and **late-position phrases need nothing** (`hard_signal()` already searches the whole
text; the misses are vocabulary misses, not position misses).

### 2. ~~Run the curated-negative bucket under `p7`~~ — DONE 2026-08-03

Both shipped voters ran the 400 curated negatives under `p7`. The result did not confirm
the 64% headline; it bracketed it. Net of the override the tier discards **7%** of that
bucket against **60%** of the screen-confirmed one (11% and 64% before the override was
widened), because the override fires on 83% of curated negatives against 36% of
screen-confirmed ones. See *What the tier actually saves depends on the bucket* in
`REPORT.md`.

This is why §1's additions were priced against negatives as carefully as positives: on a
vocabulary-rich population the override erases most of the tier's saving, so no pattern
gets waved through at $0.0018 a row.

### 3. The shadow run — the only thing that settles this, and the only thing still open

`PRESCREEN_MODE=shadow` over fresh rows, then count how often the tier would have
discarded a row the validated screen went on to keep. Everything measured so far uses
gold positives that are the *easy*, canonical FLoRA entries, and negatives labelled by
the very screen under test. Codex's strongest point: also human-review a random sample of
rows where the pre-screen AND the screen both say discard — otherwise you only learn that
two correlated LLM systems agree.

It is open because it is not a desk task: it needs a real Stage 3 pass over fresh rows,
which spends Gemini and OpenAI screen budget, so estimate and clear the cost first. It
also answers the two questions everything above leaves hanging — where in the $3–$30
band the tier actually lands, and whether tier B is worth adding.

### 4. ~~Re-check `inclusionai/ling-2.6-flash`~~ — DONE 2026-08-03

The Novita endpoint answers again; all four buckets are run. It discards as hard as
mistral-small (96% / 82%) at a fifth of the price, and `ling + mistral-small` now loses
no gold positive and nets marginally more than the shipped pair. It is **not** swapped in:
alone it misses one gold positive in twenty, five times qwen's rate, and its zero rests on
a regex pattern added the same day. Keep it as the candidate for the day the tier's own
cost becomes the binding constraint — one env var. Details in `REPORT.md`.

## Gotchas that cost me time

- **Use the venv**: `/Users/lukaswallrich/Documents/Coding/flora-extractor/.venv/bin/python`.
  System python3 lacks `filelock` etc., and `shared/prescreen.py` imports the world.
- **`data/` only exists in the main checkout**, not in the worktree. Eval scripts use
  absolute paths to it. The 5 failing tests in `tests/test_analysis_overlap.py` and
  `tests/test_apa_resolver.py` fail for this reason on main too — they are pre-existing,
  not yours.
- **API keys** are in `~/.claude/api_keys.env`, not `.env`. `set -a; source …; set +a`.
- **OpenRouter credits**: $9.25 left of $35 (checked 2026-08-03, `GET /api/v1/credits`).
  The whole pre-screen eval has cost well under $1 of that; a depleted balance returns
  **HTTP 402**, which is easy to misread as a model problem.
- **A 429 wall can be temporary.** `inclusionai/ling-2.6-flash` returned 429 to every call
  on 2026-08-02 under every routing mode and answered normally the next day. Retry a
  written-off model before concluding it is unusable.
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
| `OVERRIDE_EVAL.md` | the override measured on 7,505 papers; which patterns pay, which were rejected |
| `README.md` | how to run the harness, the population, the split rule |
| `CASESETS.md` | how the four buckets were built and their caveats |
| `override_positives.json`, `override_negatives.json` | the 7,505 / 1,333 override corpora |
| `build_override_sets.py`, `eval_override.py` | build and score them; no LLM calls, ~6 min |
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
