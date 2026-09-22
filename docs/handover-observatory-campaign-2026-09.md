# Handover — the Metascience Observatory campaign, 2026-09-21 → 22

What was done with the Observatory's replication list, what it cost, what it found, and
the email draft that reports it. Every number below was read off an artifact on this
box; the commands that reproduce each are named.

This supersedes the sequence half of `handover-observatory-screen.md` (its steps are all
run); that file's *Environment* section is still the reference for the pool and cache
gotchas.

## What the Observatory list is

`analysis/mo_observatory/replications_database_2026_09_04_184008.csv` (gitignored,
13 MB; download URL in `PENDING_RUNS.md`). 5,042 replication DOIs, each with one
original (`original_url`), a four-value `result` (success / failure / inconclusive /
reversal), authors, years. Against FLoRA's 1,837: 1,378 in both, 459 FLoRA-only, 3,664
Observatory-only. The Observatory imports FLoRA, so the 75% coverage of ours is partly
circular.

Two populations were treated separately and must not be confused:

| | works | where |
| --- | ---: | --- |
| Observatory DOIs that are **works in our survivor pool** | 2,396 | this campaign — Stage 2 rule `curated-observatory`, then Stage 3 |
| Observatory DOIs **not in the pool** | 710 | `analysis/mo_observatory/` offline scripts, done before 2026-09-21 (PR #208); 117 agreed rows already appended to the FLoRA entry sheet |

## The campaign, step by step

| Step | Command / commit | Result |
| --- | --- | --- |
| Merge #208 | `gh pr merge 208 --merge` → `b72ad97` | the `doi_in` match key and the candidate spec |
| Promote the rule | `git mv analysis/mo_observatory/curated-observatory.json filter/spec/` → `eb9b892` | pile `screen_expensive`, precedence 745; policy table + route corpus tests updated |
| Route | `.venv/bin/python -m filter.engine route` | release **`c048ab6483d3`**; `screen_expensive` 7,760 → 9,105 (+1,345; predicted +1,346) |
| Screen | `… screen --tier screen_expensive --run --release c048ab6483d3` | 1,469 works, both votes each; **1,346 proceed / 123 discard (92%)**; ≈$2.3 |
| Extract | `… extract.tier --run --release c048ab6483d3 --only "$(cat analysis/mo_observatory/extract_only_2026-09-21.txt)"`, three runs | 1,198 / 1,198 have a row; two runs stopped on the 9.5M/day OpenAI cap, the third ran with `OPENAI_DAILY_TOKEN_BUDGET=0` on the command line; ≈$4.1 of gpt-5.6-luna |
| Export | `… extract.export --release c048ab6483d3` → `61aaeb9` | `extracted.csv` 3,040 → **4,141 rows, 908 new works** (94% `link_confidence` high) |

Where the 1,198 campaign works landed (counted off `data/*.csv`): 908 `extracted.csv`,
224 `target_pending`, 76 `search_link_unconfirmed`, 22 `unidentified_original`, 12
`no_original_found`, 8 `keyed_link_disputed`.

The extract dry run for the release now offers 27 works, all of them the DOI twins
below — that is the expected end state, not a backlog.

## Three things found on the way (all fixed or filed)

1. **Python 3.12 moved the prompt hashes** (`588f31a`). `prompt_version()` hashed
   `ast.unparse()` output, which 3.12 renders differently for f-strings; on this box the
   extract generation read `396456b852b566d9` instead of `010cf32bb63351e1` and the dry
   run offered all 5,928 settled works. Now hashed from raw source; `_FROZEN_VERSIONS` in
   `shared/prompts.py` keeps every on-disk key valid. Any interpreter is fine now.
2. **The screen dry run priced the whole pile** (`588f31a`). It printed $13.93 for
   9,105 rows while `--run` bought 1,469 for $2.31. It now subtracts decided works and
   says which it priced.
3. **30 DOI twins** — issue #210. OpenAlex records carrying a real replication's DOI
   under an unrelated paper (a 1948 cosmic-ray paper under a JEAB 2010 DOI). `doi_in`
   cannot see them, the screen passed several, extraction fuses two papers. Listed in
   `analysis/mo_observatory/doi_title_mismatch.csv`; the 27 in the worklist are held
   out by the `--only` file and still need a discard rule. Only the curated rule's
   admissions were checked; the pool-wide audit is open.

## Agreement with the Observatory

`.venv/bin/python -m analysis.mo_observatory.in_pool_agreement` → `in_pool_agreement.csv`
(1,588 rows where both databases carry the paper, 1,303 works; includes the 579 in-pool
Observatory works that were already admitted before the rule).

| | |
| --- | --- |
| same original DOI, per row | 69% — misleading: a paper with two originals on one side scores one "miss" |
| **same original, per work (any in common)** | **84%** (1,101 / 1,303) |
| works naming different originals, both DOIs | 181; 25% share a first-author surname, 62% are within a year → different papers from the same period, i.e. multi-original replications, not blunders; ours at high confidence in 201 / 223 rows |
| same outcome, given same original | **74%** (812 / 1,101); the residue is mostly success ↔ inconclusive (56 + 59 rows); **12** flat success/failure contradictions |
| out of pool (earlier work, `scope_differences.md`) | 76% same original, 79% outcome agreement, 4 contradictions; screen passed 308 of 573 (54%); 147 of the discards' abstracts say "we replicate" but OpenAlex holds no abstract for them |

Outcome vocabulary map for the comparison: `TO_MO` in `build_flora_entries.py`
(successful→success, failed→failure, mixed and statistically-successful-but-flawed→
inconclusive; our `cannot_be_determined` stays unmapped and counts as disagreement).

## Still open

- `PENDING_RUNS.md`: the 14 `candidates-*.parquet` files in the shared pool repo.
- #210: a discard for the DOI twins, and the pool-wide audit.
- The `curated-observatory` spec says to **delete it** once its works are screened and
  extracted — they are. Deleting mints a new release; do it before the next route and
  read the spec's description first.
- `~/.venvs/flora313` was a workaround and can be removed.

## Email draft to Dan Elton (latest, 2026-09-22)

Written for someone who does not know our pipeline; one sentence per finding. Two
choices left to Lukas: whether to mention the 117 rows already in FLoRA's validation
queue (currently not mentioned), and confirming the 4 September snapshot is the one he
sent.

> **Subject:** Observatory × FLoRA — first overlap numbers, and a call in October?
>
> Dear Dan,
>
> Thanks again for sharing the Observatory database. We have now compared it with FLoRA
> and run your entries through our own automated extraction, and here is the short
> version.
>
> Of your 5,042 replications, 1,378 are also in FLoRA (which has 1,837 in total, 459 of
> them not in the Observatory), and we have now processed the roughly 1,200 further
> papers of yours that our automation could locate but had not identified as
> replications by itself. Your definition of a replication is clearly broader than ours
> — our classifier rejects about half of the papers it hadn't found itself, mostly
> because the paper does not state an aim to re-test a specific earlier finding — and
> the comparison also exposed a real gap on our side: 147 of your papers do say "we
> replicate…" in their abstract, but we never saw it because our search runs over
> OpenAlex metadata, which carries no abstract for those papers. Where both databases
> cover a paper, 84% name at least one original study in common; the remainder are
> mostly two confident choices of different papers from the same period, which looks
> like replications with several originals rather than errors, though only a human
> reading would settle it. Where we agree on the original, we also agree on the outcome
> 74% of the time, and nearly all the rest is "success" versus "inconclusive" — only 12
> pairs flatly contradict.
>
> Could we find an hour in October to talk through where our two approaches differ —
> ideally over a small shared sample we both check by hand — and about how to move
> towards an integrated replication database? One thing to keep in mind from our side:
> FORRT now has two products, FLoRA as a list of replications and FReD as a detailed
> effect-size database, and we want to keep both, since they serve different purposes
> and cost very different amounts to maintain.
>
> Best wishes,
> Lukas
