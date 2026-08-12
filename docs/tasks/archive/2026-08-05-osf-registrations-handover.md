# Handover — OSF registrations, 2026-08-04/05

Everything from the OSF work that a later session needs and cannot read off the code.
Shipped work is in `analysis/osf_registrations/REPORT.md` and the two
`filter/spec/osf-registration-*.json` descriptions; this file is the open items, the
decisions and their reasons, and the things that are true but surprising.

## Where it stands

| | state |
| --- | --- |
| The OSF backfill source + the two specs | on `main` (#162) |
| The census, the promotion refusal, the `flora_skip` URL fix | on `main` (#163) |
| `osf-registration-protocol` (the discard) | **`shadow: true`, promotion refused** |
| The COS Google Sheet | **written** — `alt_identifier_r` added, `url_r` repointed |
| fred-data pipeline fix | merged (forrtproject/fred-data#137) |
| `flora.csv` carrying `doi_r_alt` for RPP | **unverified** — see "Owed" below |

## Decisions taken, and why

**The discard rule stays in shadow, and it is not one enumeration away from promotion.**
The census passed every cheap gate — thirteen templates, all non-keep ones
pre-data-collection, 0 known FLoRA papers among the 1,308 discards over the whole
population. What refused it was one record in a 300-row read: `10.17605/osf.io/pr8a4`,
a completed replication of Kerner's FDI models written up in past tense on the EGAP
template. One real study in 300 of 1,308 estimates ~4.4 papers lost.

**The obvious narrowing is closed off.** `pr8a4`'s own structured timing field says
"Registration prior to researcher access to outcome data", contradicting its content, and
`6me7j` shows the template name and the timing field disagreeing the other way. **Do not
build a narrowing on OSF's timing fields.** The remaining candidate is a past-tense
results-reporting construction in the record's own text, which needs its own sample.

**Post-data-collection templates do not admit** (maintainer, 2026-08-04): registering
after collection still registers a design. The census then found no such template exists
anywhere in the 1,674 registrations, so the arm cost nothing — a count, not a judgement.

**RPP reports belong in FLoRA as one row per replication with all identifiers**
(maintainer, 2026-08-05). This is what drove the sheet work below.

## Three things that are true and were not expected

1. **FLoRA identifies an OSF record by URL five times more often than by DOI** — 366 rows
   by `url_r`/`url_o`/`oa_url_*` against 51 by DOI, 9 both. `load_flora_skip_dois()` read
   DOIs only, so ~357 records FLoRA already holds were invisible and Stage 3 would
   re-extract them. Fixed by `_osf_doi_keys()` in `shared/flora_skip.py`; it is the one
   place the GUID→DOI mapping lives and both the skip list and the census read it.
2. **The Open-Ended keep arm matches on the TITLE, in 99% of the 336 rows it admits** —
   not on the responses form, as the spec originally implied. What it reaches is almost
   entirely RPP registrations whose whole text is "Registered prior to RPP publication".
3. **The RPP `url_r` pages have no DOI at all** — 0 of 91; `api.osf.io` identifiers empty
   and the constructed DOIs 404 at DataCite. OSF mints DOIs for registrations and
   preprints, not project pages. What each report has instead is a **post-completion
   registration snapshot** that does have a resolvable DOI: 118 of 129 registered in July
   2015, weeks before RPP published, whose file listing is `Archive of OSF Storage` — a
   frozen copy of the node holding the final report. It is the report, archived.

## What was written to the COS sheet, and how to undo it

Sheet "Validating FReD replication success - Brinna", tab `replication success`
(`1J9Lp_RF6hqlDlLT3aMW3zvbQ3GYGbmFzZYdFc7XXwhc`, gid 984458430) — the
`FReD_FOUNDATION_SUCCESS_CODING` source in `prepare_flora.qmd`. 337 cells:

- `Y2` = `alt_identifier_r` (column Y was entirely empty; data stops at X).
- 168 of the 184 RPP rows: `Y` = the snapshot DOI(s), comma-separated where a node has
  several; `H` (`url_r`) = `https://doi.org/10.17605/OSF.IO/<GUID>` using the earliest.
- 16 rows (8 report nodes) untouched — no registration exists, so no DOI, and none was
  invented.

**Reversal**: `analysis/osf_registrations/cos_sheet_backup.csv` holds every RPP row's
prior `url_r` against its sheet row number. `cos_sheet_plan.csv` is the exact change.

Checks that were run first, and should be re-run before any similar edit: all 117 distinct
DOIs verified to resolve at DataCite; the exclusions sheet matches on `url_r`
(`!(url_r %in% exclusions$url_r)`) and was confirmed to name none of the 91 nodes.

## Owed

1. **Verify `flora.csv` end to end.** The pipeline re-run after fred-data#137 was still in
   flight at handover. Check `output/flora.csv` for: 92 RPP rows still 92 (the dedup keys
   off `url_r`, which changed — this is the number most worth confirming), 84 with
   `doi_r_alt` populated, 8 without. Then refresh the local `data/flora.csv`.
2. **Re-measure the RPP duplicate question once `flora.csv` updates.** 42 of the 95
   parseable Open-Ended admits name an original FLoRA already lists under the aggregate
   RPP paper. `load_flora_skip_dois()` already reads `doi_r_alt`, so once the registration
   DOIs land there, some of our pool's admitted registrations will be skipped
   automatically — but only where the pool row's GUID equals a snapshot GUID, and the
   overlap was 48 of 336 when measured from the other direction. Count it, don't assume.
3. **`filter/spec/rule_ideas.md` §2b still says the population holds 8 known FLoRA papers.**
   It is 16 once URL-named OSF records are counted. The REPORT and both specs were
   corrected; §2b's "two numbers are wrong" paragraph was written before the guid fix and
   names 8.
4. **The eight uncovered report nodes** have no OSF identifier of any kind. Nothing to do
   from our side; recorded so nobody re-derives it.
5. **`data/flora.csv` was stale from 2026-08-01 to 08-05** — the daily fred-data run had
   been failing on the `rename_with()` bug. Every recall number quoted in #162/#163 was
   measured against that stale file. Nothing changes as a result (the RPP rows are
   long-standing), but any number re-derived now may differ from the reports.

## Process notes worth keeping

- **Check out HEAD in a detached worktree before trusting "the tests pass."** `c6b4c5c`
  committed `filter/engine/backfill.py` without the `search/fetch_abstracts.py` it imports
  from; the dirty working tree passed and a clean checkout failed at import. Caught only
  because the checkout was done.
- **This repo is worked in parallel.** Several times during this session the spec bundle,
  `tiers.py` and the test suite changed underneath. Commit only your own paths, and
  attribute failures before reporting them — a 40-test failure here was the
  `replication-claim-title` → `-broad`/`-strong` split, not the work in hand.
- **Stacked PRs**: merge the parent WITHOUT `--delete-branch`, rebase the child onto the
  new main (`git rebase --onto origin/main <old-base>`) because the parent was squashed,
  then retarget and merge. Deleting the parent branch first auto-closes the child.
