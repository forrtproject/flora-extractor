# Handover: duplicate-blocking at the handoff, and the first Stage 3 run

Written 2026-08-04 for the agent picking this up. Two tasks, in order — task 2
depends on task 1 being done, because running Stage 3 first would spend money on
papers that are already in the validation tables.

Read `CLAUDE.md` first. The two rules that bind hardest here: **estimate and
confirm cost before any paid run**, and **count on disk, never from the code**.

---

## Where things stand

**Branch** `feat/rulebook-v2-osf-templates`, three commits ahead of `origin/main`
(`5747f2d` engine concurrency, `cdb1c4b` analysis tooling, `c6b4c5c` the rule
bundle). Nothing pushed. Test suite: **1077 passed, 1 skipped, 2 xfailed**.

**The routing release is `93b6d1acbc3c8e6d6fbb1856bd039d8a827b37a6b975315c254f8ee9e9c0f25e`**
(pass it in FULL — a 12-character prefix silently selects zero rows). It was
routed under a bundle that has since changed, so the store holds a stale release
relative to `filter/spec/`. **Do not re-route casually**: a new release id would
orphan the claims that carry tonight's screening. Verdicts survive a re-route
(`decided_work_ids()` is keyed by tier, not release) but claims do not.

**The screen is complete for the live tier.** 1,870 works routed to
`screen_expensive`, 1,867 decided: **1,834 proceed, 33 discard**, 3 left with a
single vote. Vote labels across 3,737 rows: replication 3,379, both 113, none
100, reproduction 80, unclear 62.

**Deduplication state.** `record_metadata.work_id` was NULL on all 1,770 rows and
is now populated on 1,739 of them, resolved from the local pool at no API cost.
The 31 still NULL are deliberate: 17 DOIs resolve to more than one work id
(merged OpenAlex records — `filter/spec/aliases.json` is the mechanism for
those), 13 are not in the pool, 1 has no DOI. **Filling the column exposed 4
work ids that each carry two `record_metadata` rows** — pre-existing duplicate
validation records, not created by the backfill. Worth reconciling.

Against that key: of the 1,834 proceed works, **135 are already in the validation
tables and 1,699 are new**.

**Loose ends you may trip over.** 3 works carry a single vote —
`analysis/repair_single_vote.py` fixes that additively for about half a cent.
3,545 response blobs sit `response_pending_upload` with every blob present on
disk; `python -m filter.engine reconcile` (dry-run by default) pushes them in
~36 batched commits. Neither blocks these tasks.

---

## Task 1 — stop duplicates and unscreened rows reaching Stage 3

### 1a. The constraint that decides the design

**A spec cannot do this.** Specs match on DOI, title, work type, concepts and
abstract presence. They cannot query Supabase at routing time. More
fundamentally, `docs/filter-engine.md` states the invariant that routing is a
pure function of `(pool text, text revision, filter bundle, engine version)` —
"already in the validation tables" is none of those. It is mutable external
state, and putting it in the routing table would make routing non-reproducible
and would be erased by the next `route` anyway.

**So: not a routing pile.** The maintainer asked "maybe we need a separate
duplicate pile" — the honest answer is that a *pile* is the wrong shape for it,
for the reason above. If a pile is wanted regardless, the only sound way is to
materialise the validated work-id list into the bundle the way `aliases.json`
is materialised, with its own hash feeding the release id. That is a real design
and it is heavier; raise it with the maintainer rather than choosing silently.

### 1b. The seam that IS right: `filter/engine/handoff.py`

`handoff.decisions(client, release_id)` already returns `(drop, screen)`
computed from live verdicts, and `write_handoff()` leaves the dropped works out
of `data/filtered.csv`. The module docstring is explicit that **live verdicts are
applied here and not in the routing table**, precisely because routing is derived
data. Duplicate-blocking belongs in exactly that seam, for exactly that reason.

What to build:

- Read the validation tables' `record_metadata.work_id` (now populated) and drop
  those works from the handoff.
- **Do not drop them silently.** A dropped-as-duplicate row and a
  screen-discarded row are different facts and must not look alike. Write the
  duplicates to a sidecar (`data/filtered-duplicates.csv` or a column in the
  manifest) so the count is auditable and a wrong match is recoverable. The
  engine's whole style is that absence must be explainable.
- Key on `work_id`, not DOI. 416 of the 1,834 proceed works have **no DOI at
  all**, so a DOI-keyed check would miss almost a quarter of them. (The DOI-based
  count says 135 overlap; the work_id count says 135 too, which is a good sign,
  but only work_id covers the no-DOI rows.)
- Handle the 31 NULL work_ids: a validation record with no work_id cannot be
  matched, so it cannot block anything. Report that number rather than pretending
  coverage is complete.

### 1c. Unscreened rows must not progress

Today `write_handoff()` exports **both screen piles as routed**, and only leaves
out works a *live* tier run discarded. A work with **no verdict at all is
exported**. That is currently harmless — the live tier is fully screened — but it
is exactly the leak the maintainer wants closed: the moment a tier is partly
screened, unscreened rows flow to Stage 3.

Change it so a row reaches `filtered.csv` only on a live `proceed`. Two things to
get right:

- The docstring currently promises that "without Supabase configured there is
  nothing to read, and the piles hand off exactly as routed". Requiring a verdict
  inverts that: with no Supabase, nothing would export. Decide deliberately and
  say so in the docstring — a flag (`--screened-only`, default on?) is probably
  cleaner than changing the meaning of the existing behaviour silently.
- `screen_cheap` is empty in this release (all three cheap rules are shadow), so
  test the change against the synthetic bundle in `tests/engine_bundle.py`, not
  only against live data.

### 1d. Related but different, do not conflate

`extract/run_extract.py` already has `--skip-flora-validated` and
`shared/flora_skip.py`, which skip papers already in **`data/flora.csv`** — the
published FLoRA database. That is a different list from the **Supabase validation
tables** this task is about. Both exclusions are wanted; keep them separate and
say which is which in any message you print.

### 1e. Verification

- Unit: one test per seam (a duplicate is dropped and recorded; a work with no
  verdict is not exported; a validation record with a NULL work_id blocks
  nothing). Mock Supabase — no live calls in `pytest`.
- End to end: run `handoff` and confirm the row count falls by the number of
  duplicates you report, and that the sidecar accounts for the difference exactly.
  **Count on disk**: `wc -l data/filtered.csv`, do not infer from the code.

---

## Task 2 — run the remaining entries through Stage 3

Only after task 1, so the 135 duplicates never reach it.

### Sequence

```bash
python -m filter.engine handoff --out data/filtered.csv \
    --release 93b6d1acbc3c8e6d6fbb1856bd039d8a827b37a6b975315c254f8ee9e9c0f25e
python -m extract.run_extract --limit 25        # pilot FIRST
python -m extract.run_extract                   # only after the pilot is read
```

Expect roughly **1,699 rows** to reach `filtered.csv` if the duplicate drop works
as measured. Verify the number rather than trusting this note.

### Cost — the part that needs care

Stage 3 is **much** more expensive per row than the screen: it acquires PDFs,
runs the target-identification LLM over abstracts, reference lists and sometimes
full text, and then codes an outcome. The screen cost about $7 for ~1,870 works
across two voters; Stage 3 on 1,699 rows is a different order of magnitude and
has no dry-run estimator equivalent to the tier's.

**Do the pilot first, measure, then extrapolate and confirm with the maintainer
before the full run.** `cache/token_usage.json` records per day/provider/model;
diff it around the pilot to get a real per-row cost. `OPENAI_DAILY_TOKEN_BUDGET`
(default 8,000,000) will stop the run cleanly if it is exhausted — that is a
backstop, not a plan.

Also relevant: OpenAlex is metered and **not** uniformly. A title search is 10×
a filter query and full-text download is 100×; `llm_title_search` sits at the
bottom of the resolution ladder partly for that reason. A Stage 3 run over 1,699
rows will move real OpenAlex credit, not just LLM spend.

### Two things that may bite

- **416 of the proceed works have no DOI.** Stage 3's row identity falls back
  `doi → oa: → url: → title:` (`shared/row_key.py`), so they are not fatal, but
  `link_original.py` and `doi_verify.py` lean on DOIs. Check on the pilot how
  no-DOI rows behave before assuming the full run will handle a quarter of its
  input gracefully.
- **Recent-heavy distribution.** The proceed set has a median year of 2021 (978
  works 2020–2024, 305 from 2025 on, only 85 before 2010). Recent papers are more
  likely to be behind a paywall with no OA copy, so PDF acquisition failure rates
  may be higher than historical runs suggest.

### Verification

`extract/sanity_check.py` runs automatically on completion and on Ctrl-C, and
quarantines suspect rows to set-aside CSVs. Read what it quarantines rather than
only the headline count. Then `python -m extract.audit_extracted` is the
read-only pre-validation audit.

---

## Environment

`.env` is gitignored and now holds: `GEMINI_API_KEY` (the paid key — voter 1 of
the screen), `OPENAI_API_KEY`, `SUPABASE_URL`, `SUPABASE_SERVICE_KEY`,
`SUPABASE_ACCESS_TOKEN` (Management API, used to apply `db/migrations/*.sql`),
`HF_TOKEN`, `FLORA_POOL_REPO`, `ELSEVIER_API_KEY` (Scopus — entitlement is
IP-bound, so it 401s off the subscribing network). `OPENAI_USE_FLEX` and
`GEMINI_USE_FLEX` are both on: 50% off each, with fallback to standard.

`ClaimsClient()` needs `shared.config` imported first or it will not see the env.

Both engine migrations are applied to the Supabase project (`Floraa`,
`eu-west-1`). A release must be **registered server-side** before it can be
claimed — `route` does not do this, and the RPC rejects an unknown release:

```python
import shared.config, json
from filter.engine.claims import ClaimsClient
rec = json.load(open("cache/engine/releases/<release_id>.json"))
ClaimsClient().register_release(rec)
```

If a run is interrupted it leaves an `active` claim that blocks the next attempt
with "nothing to claim". Release it as `failed` before retrying.

---

## Open questions for the maintainer, not for you to decide

1. Duplicate **pile** versus handoff-time drop (§1a) — the pile costs the routing
   purity invariant.
2. Whether `handoff` requiring a verdict should be the default or a flag (§1c).
3. The 4 duplicate `record_metadata` pairs — merge, or leave and let the drop
   handle them?
4. The 17 merged-DOI work ids: populate `aliases.json` and re-resolve, or leave
   NULL?
