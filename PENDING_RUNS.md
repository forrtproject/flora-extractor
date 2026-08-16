# Owed operational runs

**Committed code that has not yet been applied to the artifacts.** Every entry here is
a run someone still has to make before the change it belongs to is real.

This file exists because the pipeline's state is not in git. The verdict store, the
survivor pool, the text overlay and the API caches are all outside the repository, so
`git status` — the one check every session makes — cannot show that a run is owed. A
committed fix therefore looks finished while the artifact it fixes is still stale.
That is how issue #196's overlay backfill sat unrun: `overlay.py::worklist()` covered
the missing registrations, the commit landed, and 1,115 admitted OSF works kept
reaching the screen with no registration text in front of them.

**How it is enforced.** `.claude/settings.json` reads the open entries below at session
start and before every prompt. It never blocks a command — it states what is owed, so
the work cannot be reported as complete while an entry is open.

**How to use it.** Add an entry in the same commit as any change whose effect needs a
run. Tick it off (`- [x]`) in the commit that records the run's result. An entry needs
the command, pasteable from the project root, and what proves it worked.

---

## Open

(none)

## Done

- [x] **Re-screen every screened work under the new voter pair and gate** (voter 1
      is now `deepseek/deepseek-v4-flash@low`, the gate G-unanimous; evidence:
      `analysis/screening_eval/cheap_voter_2026-08.md`). The voter swap minted a new
      screening generation, so every screened work is claimable again and **the
      extract tier's worklist offers nothing until this runs** — it holds back every
      work without a current-generation screen verdict, which makes this a
      prerequisite of the issue #198 re-extract below.
      `.venv/bin/python -m filter.engine screen --tier screen_expensive --run --release 56076eb`
      — `--release` is required, and release `8b3d` refuses on the rebuilt overlay.
      If the OSF projects backfill below runs first, its re-route mints a newer
      release; name THAT one here and in the extract entry instead, or the OSF works
      whose text it recovers are screened twice.
      Expect roughly $1.80 of DeepSeek spend for ~10k works (measured
      $0.070/390 calls at effort low; halved off-peak outside 01:00–04:00 and
      06:00–10:00 UTC from 2026-08-16); the gpt-5.4-mini side reads the joint-era
      cache entries — including the model-less majority shape, lifted by provider —
      and costs nothing.
      Before the full run: the 20-work smoke test
      (`… screen --tier screen_expensive --run --limit 20 --mode validation --release 56076eb`)
      must show no missing votes and a sane per-call wall clock — it is what checks
      the OpenRouter provider pinning (`require_parameters` + throughput floor)
      leaves an eligible host for every model.
      Done when `filter.engine status` shows the screen tier settled for the release
      and the extract worklist is non-empty again.
      **Done 2026-08-14/15**, release `16d370746b45` (minted by the combined re-route below): 7,760 works screened, 880 discard / 6,880 proceed; DeepSeek ≈$2 (9.0M in / 1.8M out). The extract worklist reopened at 5,928 works.

- [x] **Reopen the 55 works that shared one OSF identifier** (issue #201). Until
      `681556a`, `osf_registration_guid()` read the path segment in front of a guid as
      the guid, so every pool work with a download-shaped URL keyed
      `osf:osf.io/download`. That key is on disk as a definitive miss: the 55 were
      asked about once, between them, and all still wear that one answer. The code is
      fixed; the checkpoint is not.
      `.venv/bin/python -c "import shared.config; from shared import abstract_store; print(abstract_store.drop_misses(['osf']))"`
      then re-run the OSF backfill phase as the Done entry below records.
      Done when no work resolves to a guid of `download`, `preprints` or `project`, and
      the re-run reports a non-zero "Abstracts found".
      **Done 2026-08-13**: the misses were dropped and the OSF backfill re-run; the re-route below carried the recovered text.

- [x] **Freeze and re-route, carrying the OSF text and the no-text exemption**
      (issue #196). Two committed changes reach nothing until a route reads them:
      `overlay-0007.parquet` (57 project descriptions) is written but not frozen, and
      `e0feb7d` exempts titled OSF records from the `no_text` downgrade.
      `.venv/bin/python -c "import shared.config as c; from filter.engine.overlay import freeze; print(freeze(c.OVERLAY_DIR)['overlay_hash'])"`
      then `.venv/bin/python -m filter.engine route`.
      Expect roughly 849 works to move out of `pending/no_text` into a screening pile.
      **Carry the issue #200 aliases in the SAME route if they are ready** — each
      re-route mints a release, and screening between two of them pays twice for works
      about to be merged.
      Done when the route report shows `pending` down by ~849 and the new release id is
      recorded in the re-screen entry above.
      **Done 2026-08-14**: frozen and re-routed together with the issue #200 aliases; release `16d370746b45`, overlay hash `011bba95582f` (9 chunks), 7,760 works in `screen_expensive`.

- [x] **Deduplicate OpenAlex works that name one OSF record** (issue #200). One OSF
      record ships as several rows: 117 records in `data/extracted.csv` are reached by
      more than one work id, 296 of 2,602 rows (11%). The aliases ARE derived and
      merged: `filter/spec/aliases.json` holds the 14,744 same-guid entries
      (`analysis/build_osf_aliases.py`, commit `3fb6efe` — adjudication and QC in its
      message; 405 groups excluded as OpenAlex mis-located works). What remains is the
      route that applies them — the combined re-route above — and the export check.
      Done when a re-route has collapsed the duplicates and the export's row count
      falls by the merged surplus (expected ≈165 rows on the current CSV).
      **Done 2026-08-16**: the export of release `16d370746b45` shows OSF records reached by more than one work id down from 92 (223 rows) to 6 (13 rows). The total row count is not the proof any more: the re-screened gate admitted 928 works no earlier render held, so the main CSV rose to 3,147 rows.

- [x] **Re-extract every settled work under the new outcome policy** (issue #198).
      The outcome prompt now codes as the authors report: an overall author verdict
      decides; otherwise their comparisons to the named original decide; a result with
      no stated bearing on that original's finding is `cannot_be_determined`; and
      «descriptive» needs the authors' own account of reusing the methods. Editing it
      minted a new extract generation, so every settled work is already reopened and
      the shipped CSV still carries verdicts coded under the old policy.
      `.venv/bin/python -m extract.tier --release 56076eb --run` then
      `.venv/bin/python -m extract.export --release 56076eb`
      (release `8b3d` refuses on the rebuilt overlay; if the OSF backfill re-routes
      first, name its release instead — the export now refuses a stale release too).
      Before the run: `.venv/bin/python -m analysis.purge_osf_docs --apply` (re-rank
      the cached OSF storage files under the plan-file demotion) and
      `.venv/bin/python -m analysis.purge_epmc_retries --apply` (unsuppress the fixed
      Europe PMC tier), plus `.venv/bin/python -m shared.cache_sync --pull --parts doi_verify`.
      Both purges print what they would do without `--apply`.
      Expect the run to re-parse the ~900 cached documents: `TEXT_EXTRACTION_VERSION`
      is at 2, so every parse entry written under the old page window and section
      splitter is a miss. That is local compute (~7.5 s per document, ~30 min at
      `EXTRACT_WORKERS=4`) and buys no LLM call — the reference extractors are keyed
      on the prompt, the model and the PDF's content hash.
      The outcome-family LLM calls are re-bought; the link picks, keyed confirms and
      search grades are cache hits, and DOI verification is ≈0 OpenAlex credits (its
      caches carry no generation — check `print_search_summary()` at the end came out
      near zero). Sandbox-measured on 25 works before commit: 17 of the 20
      stratified rows unchanged, no `successful` or `failed` row pushed to
      `cannot_be_determined`.
      Done when the export prints NO `rows from a superseded generation:` line —
      `--check` alone does not test carry-forward — and section 2 of
      `handover.html` is re-read off the new render.
      **Done 2026-08-16**: 5,928 of 5,928 works settled (1,567 under `gpt-5.4-mini`, the rest under `gpt-5.6-luna` for $6.12); the dry run reports 0 open works. Rendered with `.venv/bin/python -m extract.export --release 16d370746b45 --current-generation-only`: 3,147 main rows, 6,434 in all, `--check` zero diff. The flag is deliberate: the default render carries 96 works whose only verdict is from a superseded generation and which the worklist never offers — 85 the current screen discards, 11 on the FLoRA skip list — so the done-criterion (no `superseded generation` line) held only with it. The export now drops screen-discarded works itself, and the plain render matches the file (`--check` zero diff).

- [x] **The OSF projects backfill** (issue #196), 2026-08-13. 1,699 targets, **726
      descriptions recovered**, **57 written** to `overlay-0007.parquet`. The gap is
      the two write guards doing their job: 625 refused because the row already had a
      pool abstract an overlay row would have REPLACED (751 of 752 admitted OSF
      projects carry one, 297 past 500 chars against a median description of 252), and
      17 refused as labels — "Replication", "PCI RR submission" — that the row's own
      title already carries.
      An earlier attempt the same day recovered 0 of 1,699: OSF was down, every request
      failed from the first one, and the circuit breaker stopped the phase at 25
      consecutive transient failures without checkpointing any of them. The re-run
      waited for two consecutive healthy probes and ran at `OSF_RATE_SEC=2`.
      Read afterwards, and it changed the plan: 941 targets still have no text, but
      only 93 of those are admitted and 92 of THOSE already have a pool abstract — so
      the honest yield is one admitted textless work, plus 57 rows that were
      unroutable. That is what sent the fix to the `no_text` policy instead of to more
      text sources.

- [x] **Re-route after the OSF overlay backfill** (issue #196), 2026-08-13.
      Release **`56076eb48fda`**, overlay hash `a3ccbfb18a38` (7 chunks, 3,159 rows).
      The backfill recovered 427 of 854 OSF records; the rest are projects and
      components, which the registrations endpoint 404s on and no template line can
      ever reach. `osf-registration-protocol` matched 2,103 → 2,479, and
      `osf-registration-completed` 523 → 574. Both domain-coverage gaps shrank:
      1,685 → 1,348 and 1,162 → 774. Piles moved discard +376, screen_expensive −336.
      **Which release the next campaign names is now a decision**, not a formality:
      336 works 8b3d admitted are preregistrations `56076eb48fda` discards, so an
      export against the new release stops shipping their rows — which is the fix, and
      is also a visible drop in `data/extracted.csv`. The two open entries above still
      say `--release 8b3d`; changing them to `56076eb` is what applies the re-route to
      what ships.
