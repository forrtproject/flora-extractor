# Handover — screening the Observatory works that ARE in the pool

**Goal:** put the ~1,346 survivor-pool works the Metascience Observatory calls
replications, and that no rule currently admits, in front of the expensive screen — and
then extract whatever it passes.

Written 2026-09-21. Everything below was measured on this box against release
`2e31c9543026`; re-check any number before you spend on it.

**This is a different population from `analysis/mo_observatory/`.** That directory is
throwaway work about the 710 Observatory DOIs that are NOT in the pool and are not going
into it. Do not confuse the two: nothing here touches those, and nothing there touches
these.

**Status 2026-09-21 (evening): steps 1–3 are DONE, step 4 is part-way.** Release
`c048ab6483d3`; the screen admitted 1,346 of 1,469 (92%); extraction stopped on the daily
OpenAI token cap with ~1,048 works open. The resume commands, and the 27 junk records
the `--only` list holds out, are in `PENDING_RUNS.md`. Two bugs found that evening are fixed 2026-09-22 and noted in place: the screen dry run
priced the whole pile, and Python 3.12 moved the prompt hashes.

---

## What is already done

| | |
| --- | --- |
| PR #208 | The `doi_in` match key, `analysis/mo_observatory.py`, the candidate rule, and a fix to `arm_evidence.py`. **Open — merge this first.** |
| PR #209 | Gates the `--no-llm` keyword outcome fallback off. Independent; does not block anything here. |
| fred-data #143 | The out-of-pool works. Unrelated to this task. |
| Sheet | 117 agreed out-of-pool rows appended to the FLoRA entry sheet. Unrelated to this task. |

## The numbers to expect

Measured with `arm_evidence` against release `2e31c9543026`:

| | works |
| --- | ---: |
| Listed DOIs that are pool works | 2,396 |
| — already admitted to `screen_expensive` | 579 |
| — `pending` / `no_filter_matched`, **with text** | **1,346** ← what the rule buys |
| — `pending` / `no_filter_matched`, no text | 164 (will be downgraded to `no_text` again) |
| — `pending` / `no_text` (a claim rule matched, no abstract) | 308 (**not** rescued by this rule) |
| — `discard` | 1 |

If your route report does not move `screen_expensive` by roughly **+1,346**, stop and
find out why before screening.

---

## The sequence

### 1. Merge PR #208, then promote the rule

The spec deliberately sits outside `filter/spec/` so that scoring it minted no release.
Promoting it is the whole change:

```bash
git mv analysis/mo_observatory/curated-observatory.json filter/spec/
```

Then update the policy table in `tests/test_engine_spec.py` — it is the one place the
bundle's policy is written down, and two tests will fail until you do:

* add `"curated-observatory": ("screen_expensive", 745, None, False)` to `EXPECTED`;
* `test_the_expensive_screen_has_exactly_two_routes` asserts an exact list and now has
  three routes. Rename it and add the id in precedence order (745 sits between
  `replication-claim-title-strong` at 750 and `-title-broad` at 740).

```bash
.venv/bin/pytest tests/test_engine_spec.py tests/test_engine_route.py -q
```

**Precedence 745 is deliberate and was corrected once already.** The first draft used
970 to outrank every discard, on the argument that a curated list naming works should
beat a rule that only guessed a pattern. Measured, that claim buys exactly ONE work. Do
not "restore" it without re-measuring.

### 2. Route

```bash
.venv/bin/python -m filter.engine route
```

This mints a new release id — the bundle hash changed. **Record it**; every command
after this needs it. Check the report: `screen_expensive` up ~1,346, and
`curated-observatory` listed among the live rules with a non-zero match count.

### 3. Screen — dry run first, then run

```bash
.venv/bin/python -m filter.engine screen --tier screen_expensive --release <new>
```

No `--run` prints the open works and a priced estimate for free. (Until 2026-09-22 it
priced the whole pile — 9,105 rows ≈ $13.93 printed against the 1,469 ≈ $2.31 a run
bought; fixed the same day, and the render now says which it did.) The 7,760 works
already screened are not re-bought: `decided_work_ids()` subtracts works already decided,
keyed by a question hash, so only the newly admitted ones are offered. If the dry run
offers ~8,000 rather than ~1,300, something has changed the screening generation — find
out what before spending.

Smoke test before the real run, as the previous campaign did:

```bash
.venv/bin/python -m filter.engine screen --tier screen_expensive --run --limit 20 \
    --mode validation --release <new>
```

Check every work got two votes and the per-call wall clock is sane — that is what
verifies the OpenRouter provider pinning leaves an eligible host for the DeepSeek voter.
Then:

```bash
.venv/bin/python -m filter.engine screen --tier screen_expensive --run --release <new>
```

Rough cost: the 2026-08 campaign screened 7,760 works for ≈$2 of DeepSeek (9.0M in /
1.8M out). ~1,346 works is ≈$0.35 on that side. The `gpt-5.4-mini` side is real spend
this time — those works have never been screened, so unlike the 2026-08 run there are no
cache hits to ride on. Take the number from the dry run, not from this paragraph.

### 4. Extract

```bash
.venv/bin/python -m extract.tier                     # dry run: worklist size, free
.venv/bin/python -m extract.tier --run --mode validation --limit 20   # sandbox first
.venv/bin/python -m extract.tier --run
.venv/bin/python -m extract.export --release <new>
```

The sandbox pass is the repo's rule for the first run of changed code, and the code path
here is not changed — but the POPULATION is new, so a 20-work look before a few hundred
extractions is cheap insurance.

---

## Environment — read this before running anything

**Python version no longer matters for prompt hashes (fixed 2026-09-22).** Until then
`prompt_version()` hashed `ast.unparse()` output, which 3.12 renders differently for
f-strings, so this box's 3.12 `.venv` moved seven Stage 3 prompt versions and the extract
generation (`396456b852b566d9` for the declared `010cf32bb63351e1`) — an extraction here
would have reopened all 5,928 works. The canonical form is now the raw source minus
docstrings and comments, with `_FROZEN_VERSIONS` in `shared/prompts.py` mapping each
prompt to the version its answers are on disk under. `.venv` is fine again; check the
dry run prints `generation    010cf32bb63351e1` regardless.
**The box is set up.** `.venv` exists with every dependency; `.env` carries `HF_TOKEN`,
`OPENAI_API_KEY`, `GEMINI_API_KEY`, `OPENROUTER_API_KEY`, `SUPABASE_SERVICE_KEY`,
`OSF_TOKEN`. The pool (2,232 files), the overlay (`afc3eeb75ad5`) and the caches are on
disk, and a local route reproduces `2e31c9543026` exactly.

**The shared pool repo is contaminated, and a fresh pull re-breaks it.**
`lukaswallrich/flora-survivor-pool` holds 14 `candidates-*.parquet` files (1,363,959
rows of the retired `CANDIDATES_COLS` corpus) in the pool root. Every pool reader here
globs `*.parquet`, so they are read as pool rows — `arm_evidence` dies on one with
`ArrowInvalid: No match for FieldRef.Name(id)`. A pull also stamps
`expected_files: 2246`, so `route` then refuses the pool as partial.

On this box they are already moved to `cache/legacy_candidates/` and the sidecar
re-stamped:

```bash
.venv/bin/python -m search.snapshot_scan --stamp-pool \
    --gate d536bc51b9b26947a12869ae2dc1beb3ddb7efc606526c6c7279b914c0d3b6ab
```

If you re-pull the pool, do this again. Fixing the remote is an open `PENDING_RUNS.md`
entry and a push decision for whoever owns the repo.

**The cache manifest was broken and is fixed.** Until 2026-09-21 the remote manifest
named only `abstracts`, so `cache_sync --pull` reported "1 shard" and silently skipped
16 `cache/llm` shards (~180k LLM answers), 16 `cache/openalex`, and the rest. It now
reports **68 shards**. If a pull ever reports 1 again, the bug is back — see
`tools/repair_cache_manifest.py` and the refusal added to `push_cache`.

---

## What not to do

* **Do not put the out-of-pool works into the pool.** Decided: they stay outside the
  pipeline. The curated rule is a Stage 2 rule and reaches pool works only.
* **Do not raise the rule's precedence** without re-measuring what it buys.
* **Do not delete the rule after this campaign without reading its own description** —
  it says to delete it once the works it names have been screened and extracted, because
  a frozen list of DOIs teaches the bundle nothing and will silently re-admit works a
  later discard was written to drop. That is a real decision to make, not a formality.
* **Do not skip the dry runs.** Both `screen` and `extract.tier` print their worklist
  size and cost for free, and that is the check that says what a run will actually buy.

## Open questions this campaign will answer

The rule is a recall prior from another pipeline, not evidence about any work. Once the
screen has answered, the number worth writing down is **what share of the 1,346 it
admits**. Out of pool, on works with no replication vocabulary, the screen passed 54%.
These works DO carry vocabulary (they are in the pool, which is what the gate selects
for), so a materially lower rate would be a surprise worth investigating rather than a
result to file.
