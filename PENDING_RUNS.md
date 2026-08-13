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

- [ ] **Re-extract the 6 works the plan-document guard reopens** (ladder 26, issue #196).
      Their shipped outcomes were coded from a preregistration or an analysis plan.
      Blocked until the working tree's outcome-vocabulary work is committed: that
      change empties `_GENERATION_EQUIVALENCES`, which reopens every settled work, so
      the redo worklist reads 4,018 rows instead of 6.
      `.venv/bin/python -m extract.tier --release 8b3d --only 6925263306,6925305059,6925552412,6944082684,7043480225,7112297948 --redo 6925263306,6925305059,6925552412,6944082684,7043480225,7112297948 --run`
      then `.venv/bin/python -m extract.export --release 8b3d`.
      Done when all 6 read `cannot_be_determined` in `data/extracted.csv`.

- [ ] **Re-route after the OSF overlay backfill** (issue #196).
      The backfill of 2026-08-13 gave 854 OSF records their template line; until a
      route reads it, the two OSF specs still cannot see them.
      `.venv/bin/python -m filter.engine route` — mints a new release.
      Done when `osf-registration-protocol`'s match count has risen and the route
      report's domain-coverage gap for both OSF specs has shrunk.

## Done

_(Tick entries move here with the commit that recorded the run.)_
