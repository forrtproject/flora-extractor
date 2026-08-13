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

- [ ] **Re-screen every screened work under the new voter pair and gate** (voter 1
      is now `deepseek/deepseek-v4-flash@low`, the gate G-unanimous; evidence:
      `analysis/screening_eval/cheap_voter_2026-08.md`). The voter swap minted a new
      screening generation, so every screened work is claimable again and **the
      extract tier's worklist offers nothing until this runs** — it holds back every
      work without a current-generation screen verdict, which makes this a
      prerequisite of the issue #198 re-extract below.
      `.venv/bin/python -m filter.engine screen --tier screen_expensive --run`
      (against whichever release the campaign names — see the release note in the
      Done entry below). Expect roughly $3–5 of DeepSeek spend for ~10k works
      (halved off-peak outside 01:00–04:00 and 06:00–10:00 UTC from 2026-08-16); the
      gpt-5.4-mini side re-reads the joint-era cache entries and costs nothing.
      Done when `filter.engine status` shows the screen tier settled for the release
      and the extract worklist is non-empty again.

- [ ] **Re-run the OSF projects backfill** (issue #196). The nodes fallback is
      committed and reaches nothing yet: the 2026-08-13 run stopped on its own circuit
      breaker after 25 consecutive transient failures, having recovered 0 of 1,699
      targets. The cause is OSF, not the code — every request failed from the first
      one, including the registrations call the fallback does not touch, and a guid
      that answered HTTP 200 with a description 25 minutes earlier was answering 500 by
      then. Probably our own load: ~2,500 OSF calls in the preceding hour, and the
      fallback costs two calls per project rather than one.
      Nothing was checkpointed — the phase records a transient failure as nothing at
      all — so the run resumes with no state to repair and no misses to reopen.
      `.venv/bin/python -m filter.engine worklist --release 56076eb --out cache/engine/worklist-osf-nodes.parquet`
      then `.venv/bin/python -m filter.engine.backfill --worklist cache/engine/worklist-osf-nodes.parquet --source osf --run`.
      Check `https://api.osf.io/v2/nodes/7weum/` answers 200 first, and consider
      `OSF_RATE_SEC=2` for the re-run. Expect roughly 87% of ~1,700 to recover a
      description (26 of 30 measured by hand before the outage).
      Done when the run reports a non-zero "Abstracts found" and writes a chunk; then
      freeze the overlay and re-route, as the 2026-08-13 entry below records.

- [ ] **Re-extract every settled work under the new outcome policy** (issue #198).
      The outcome prompt now codes as the authors report: an overall author verdict
      decides; otherwise their comparisons to the named original decide; a result with
      no stated bearing on that original's finding is `cannot_be_determined`; and
      «descriptive» needs the authors' own account of reusing the methods. Editing it
      minted a new extract generation, so every settled work is already reopened and
      the shipped CSV still carries verdicts coded under the old policy.
      `.venv/bin/python -m extract.tier --release 8b3d --run` then
      `.venv/bin/python -m extract.export --release 8b3d`.
      Costs a full campaign: the LLM calls are re-bought and every resolved row pays
      DOI verification again, which is the OpenAlex daily credit budget, so expect more
      than one budget day. Sandbox-measured on 25 works before commit: 17 of the 20
      stratified rows unchanged, no `successful` or `failed` row pushed to
      `cannot_be_determined`.
      Done when `data/extracted.csv` renders wholly from current-generation verdicts
      (`extract.export --check` reports no carry-forward) and section 2 of
      `handover.html` is re-read off the new render.

## Done

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
