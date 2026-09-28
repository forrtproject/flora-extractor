# Handover — Observatory × FLoRA: fixing how originals are picked (2026-09-23)

The Metascience Observatory comparison is finished and adjudicated. What it left is
mostly **pipeline work**: our reference-list step picks the wrong original about 5–8% of
the time, "cannot be determined" is over-used, and a few smaller defects. Human
corrections of individual rows happen in the validation step (flora-validation), **not
here** — this repo fixes the pipeline and re-runs it.

Every number below was read off an artifact on this box; the script that reproduces it
is named. `analysis/mo_observatory/adjudication/README.md` lists the scripts.

---

## Status 2026-09-24 (unattended session) — read this first

Done and committed on `main`. Everything up to `6fa160d` is on origin (pushed outside this session); **the export `b31af65` and this status are local only**:

| Step | Result | Where |
|---|---|---|
| 1 | gpt-6-luna switch committed (`a4495eb`) | — |
| 2 | 14 `candidates-*.parquet` deleted from the HF pool repo (HF commit `e284f62`); a fresh pull fingerprints identical to the local pool. `~/.venvs/flora313` deleted | `PENDING_RUNS.md` |
| 3 + 4 + 5 | Sibling-rule prompt text, `_2` key suffixes, and a blind second-vendor pick check (`deepseek/deepseek-v4.1-flash`, ladder 28): a flagged pick loses `match_certain` and descends to full text. Generation `765e053cd24611e5`, declared equivalent. Sandbox pilot on 150 random non-validated `llm_references` works, **scored against blinded truth coding** (10 coder agents): wrong originals shipped live 12 → v2 4; 6 correct links lost to declines | `analysis/pick_pilot/REPORT.md`, `analysis/contrastive_confirm/REPORT.md` |
| 3.5 | **Live redo ran**: 1,599 non-validated `llm_references` works → 1,451 resolved, 136 target_pending, 8 provisional, 2 no_original_found, 2 api_error | `analysis/pick_pilot/live_redo.log` |
| 3.6 | Retirement mechanism: export appends `data/retired_pairs.csv`; `csv_to_db.py --retire` on local branch `retire-superseded-records` in `~/flora-validation` (commit `82c48da`, **not pushed**). Found: **the nightly validation sync has been blocked since 2026-09-13** (`baseline_snapshot_unavailable`) | `docs/retiring-superseded-records.md` |
| 4 (clean_doi) | Counted, not changed: ~30 values in our CSVs, 29 Supabase records; recommend a comparison-only `doi_match_key()` | `analysis/pick_pilot/clean_doi_impact.md` |
| 6 | Causes found; fix committed (`af85ea6`: full documents sent whole; full-text outcome kept for a carried original). 60-work sandbox then **live reopen of 479 cbd works** (462 resolved, 12 target_pending) | `analysis/cbd_investigation/REPORT.md` |
| export | `extracted.csv` 4,141 → 3,692 rows; replication cbd 17.6% → 9.3%; 687 retirements seeded (set aside 563: prospective 281, target_pending 175; superseded 118) — `b31af65` | `data/retired_pairs.csv` |
| 7 | Recommendation: don't fetch for the 267M no-abstract snapshot works; run the gate over the **PubMed baseline** (all 165 gap works, $0, no rescan). The real switch is promoting the shadow abstract-claim rules — live routing screens 0 of 165 | `analysis/recall_pregate/REPORT.md` |
| 8 + 9 | Shadowing `curated-observatory` would drop 908 extracted works — keep it live. DOI-twin audit over all 9,105 admitted works: exactly the known 30. `work_id_in` primitive committed; both specs prepared as patches only | `analysis/stage2_rules_2026-09/REPORT.md` |
| 10 | DOI-issue sheet built, **private** (only your account): <https://docs.google.com/spreadsheets/d/1THZn3lSpP-IGcMcKtIHz7ZIt4ReeGn1gFvZZaXiotb4/edit> — 112 issues, 149 rows | `analysis/mo_observatory/observatory_doi_issues.csv` |
| 11, 12 | No-replication-DOI note written; `report.html` regenerated with the 26 FLoRA items and corrected numbers (local, not published) | `analysis/mo_observatory/` |
| — | `flex_unavailable` 429 now falls back to standard at once (`5001d4e`) | — |

**Decisions for Lukas** (nothing is blocked on them locally):
1. **Push?** `main` is ahead of origin with the export. Pushing makes the new CSV what the
   sync imports once it is unblocked — its removal share is 16.3%, over the sync's 10% cap.
2. **Unblock the sync** (re-baseline) and approve `csv_to_db.py --retire` + the
   `retired_records` table (archive-then-delete; dry-run default). Push the flora-validation branch.
3. **Declines drop correct rows.** The redo turned ~4% of works (pilot: 6/150) from a correct
   resolved link into `target_pending`; the export retired 175 target_pending rows. Option: a
   redo rule that keeps the previous resolved verdict when the new run only declines (old
   verdict rows are still in the store, so this can be applied retroactively).
4. Not reopened, same sibling risk: `llm_cited_candidates` (263 rows), `llm_fulltext` (75):
   `--redo-status llm_cited_candidates,llm_fulltext`.
5. Promote `replication-claim-text`/`-residual` (step 7) and/or apply
   `analysis/stage2_rules_2026-09/01-add-shadow-specs.patch` at the next deliberate route.
6. Mixed-vs-success semantics with Dan; OSF prereg-only projects → prospective quarantine
   or `not_a_replication`?
7. Publish the shared caches (`.venv/bin/python -m shared.cache_sync --push`) — not done.
8. The sheet is unshared; share it and send the email yourself.

Gmail / Google Calendar / Google Drive connectors need authorising in claude.ai settings if
you want them used; this session could not (the sheet was made with the `gws` CLI).

---

## Next steps (in order)

Rules that apply to every step that spends: sandbox first
(`--mode validation`), `/code-review` on the diff before any live run, dry run (no
`--run`) to read the worklist size before buying it. See CLAUDE.md "Before a Run That
Spends".

### 1. Commit the `gpt-6-luna` switch (uncommitted in the working tree)

`shared/config.py` (`LINKING_MODEL` / `OUTCOME_MODEL` = `gpt-6-luna`), `extract/tier.py`
(`_GENERATION_EQUIVALENCES["7dbb1e92452d8333"]` keeps every settled work settled; flex
prices $0.05/$0.25 per 1M), `shared/llm_client.py`, the two test files, CLAUDE.md,
`analysis/mo_observatory/README.md`, `extract_offline.py`.
- `/code-review` the diff; `.venv/bin/pytest tests/test_extract_tier.py tests/test_llm_client.py -q`.
- Confirm `extract_generation()` prints `7dbb1e92452d8333` and that
  `.venv/bin/python -m extract.tier` (dry run) offers only the expected open works, not
  the 5,900+ settled ones.
- Commit to main.

### 2. Housekeeping that unblocks routing

- **Delete the 14 `candidates-*.parquet` files from the shared pool repo**
  (`lukaswallrich/flora-survivor-pool`, HF dataset; Lukas: "delete"). They hold the
  retired `CANDIDATES_COLS` corpus and make a fresh pull fail as partial. Use
  `huggingface_hub.HfApi().delete_files(repo_id=..., repo_type="dataset",
  delete_patterns=["candidates-*.parquet"])`, then re-stamp the remote
  `_pool_provenance.json` to `expected_files: 2232` with gate `d536bc51b9b2`
  (`search/pool_sync.py`, `--stamp-pool`). Done when a fresh `pool_sync --pull` into a
  scratch dir is accepted by `filter.engine route` untouched; tick the entry in
  `PENDING_RUNS.md`.
- **Delete `~/.venvs/flora313`.** A Python 3.13 venv made on 2026-09-21 because prompt
  hashes differed under 3.12 (`ast.unparse`); fixed in `588f31a`, so nothing needs it.

### 3. Fix the reference-list pick (`llm_references`) — pilot, then redo

**The defect.** When the paper's abstract describes its original without naming it
("a pioneer study reported previously", "our previous GWAS"), the linking model picks a
*sibling* from the reference list — the same authors' other paper, the source of the
materials, a registered-report protocol, a background citation — and marks it
`match_certain`. In 21 of 23 checkable wrong picks the correct original WAS on the
offered list. `_confirm_keyed_row` does not catch it: it sees one record, asks "is it
plausibly the named target", and is told to say no only for a different subject or
author. Cheap rules do not separate right from wrong picks (author-named-in-quote:
90% vs 87% agreement; same-author sibling on list: 87% vs 91%) — measured, see Context.

**What `gpt-6-luna` alone did** (`replay_pick.py`, cached prompts replayed unchanged):
of 56 judged-wrong picks it fixes 5, declines 27 (descend to full text), repeats 24; of
150 correct controls it keeps 136, declines 12, changes 2. Pairing it with the old
model as a second vote adds nothing — they share the same 22 wrong picks.

**Do:**
1. **Prompt change** in `build_target_outcome_prompt` / `build_repro_target_outcome_prompt`
   (shared by the abstract, reference-list and full-text rungs): name the failure modes
   explicitly next to the `match_certain` rule — several works by the same authors on
   the list; a registered report / protocol vs the original; the paper that supplied
   the materials or task vs the finding re-tested; a meta-analysis or review vs the
   primary study; a meeting abstract or working paper vs the article. And: if the
   evidence only describes the original, `match_certain` is true only when exactly one
   listed record fits the description AND no same-author record fits it too.
2. **Bundle step 4's key-suffix fix** into the same change (it also alters prompts).
3. This mints a new generation. Declare it equivalent (`_GENERATION_EQUIVALENCES`,
   with the reason) so settled works stay settled; the population is reopened by name.
4. **Pilot, 150 works, sandbox.** Draw a seeded random 150 from the `llm_references`
   works that are **not validated and not in validation** in Supabase (see Context →
   Supabase for the query; exclude `validated`, `validation_inprogress`,
   `consensus_reached`, `need_review`). Run
   `.venv/bin/python -m extract.tier --run --mode validation --redo-status llm_references --only <ids>`
   (dry run first — `--redo-status` ADDS, `--only` restricts).
   **Code the correct original for all 150 yourself** (read the paper: intro/methods,
   reference list; the adjudication's `judge_prompt.md` is the rubric) — old pick vs
   new pick vs truth. Also re-score the 56 judged-wrong and 150 control cases with
   `replay_pick.py --model gpt-6-luna` against the new prompt.
   **Decide on:** wrong originals shipped (old vs new), correct links lost to a decline
   (each costs a full-text descent), new errors introduced.
5. If better: live redo over **all** non-validated `llm_references` works (~1,570 of
   1,620), then `extract.export --release <id>`.
6. **Coordinate with flora-validation before the export is imported.** 13 of the 67
   judged-wrong works are already in the queue (unvalidated). A corrected row may get a
   new `pair_id` (if it hashes `doi_o`) — check what `csv_to_db.py` does with a changed
   row for an existing work, so the old wrong record is retired rather than left in the
   queue beside the new one.
7. After the redo, re-run `agreement.py` / `build_decisions.py`: the 67 *replace* works
   are the regression set. For any still wrong, `supersede_targets()` with the judges'
   original (patch `doi_o`, `title_o`, `authors_o`, `year_o`, append to `link_evidence`
   "original corrected by adjudication, Observatory comparison 2026-09; outcome coded
   against the previous original — re-check") — sandbox first.

### 4. Two small correctness fixes (Lukas: "do it")

- **Key suffixes.** `assign_target_keys()` disambiguates duplicate keys with a letter
  (`_suffix()`, `shared/target_keys.py:89`): `@abrahams1999b`. The model then matches
  that letter against the paper's own "(1999b)" citation — 2 of the 56 wrong picks
  (Abrahams & Mauer 1999b, Eckman 1981a). Use a non-letter suffix (e.g.
  `@abrahams1999_2`). Changes prompts → ship with step 3.
- **`clean_doi()`** does not normalise `10.1037//…` → `10.1037/…` nor URL-encoding
  (`%3c` → `<`). Both occur in our rows and in FLoRA. Before changing it, count how many
  stored rows/pair_ids/skip-list entries change (it is the identity function for
  matching everywhere).

### 5. Contrastive confirmation with a second vendor (Lukas: worth a try)

Give the keyed-record check the **whole offered list** (not one record) and ask whether
the evidence singles out the picked record over the others — blind to which was picked,
or as "here is the pick, here are the alternatives". Try an OpenRouter model from a
different vendor: Lukas suggested DeepSeek "4.1 flash" or the latest GLM flash — look up
the exact ids on OpenRouter first (the screen already uses `deepseek/deepseek-v4-flash`).
Measure offline on the same sets as step 3 (the prompts are cached; only the new call
costs). Adopt only if it catches wrong picks the step-3 prompt still lets through at a
low false-flag rate.

### 6. Why "cannot be determined" is over-used — investigate, then fix

In the adjudication, 14 of 19 of our `cannot_be_determined` rows had a result both
judges could read. On `extracted.csv` (replication rows): 18% are cbd overall — 11% with
an abstract and no document, **22% with a document**, 36% with a document but no
abstract. A document should lower the rate, not raise it. Leads:
- What evidence the coding call actually gets when a document exists — the standalone
  coder sends INTRODUCTION + DISCUSSION/CONCLUSION blocks, not RESULTS; check whether the
  target+outcome full-text rung does the same, and how `OUTCOME_DESCENT` routes a row
  that settled its link at the abstract/reference rung.
- The model's own reasons: "does not state what finding the original study obtained",
  "provides no results or verdict". The prompt may demand an explicit
  original-vs-replication comparison where a stated replication result would do.
- By link method: `llm_author_year_search` 39% cbd, `llm_title_search` 20%,
  `llm_references` 11%.
Then measure a fix on the 19 judged cbd items plus a sample, and reopen with
`--redo-status outcome=cannot_be_determined` (not-validated only).

### 7. Recall: read every cheap abstract source before the search gate

147 of the Observatory's out-of-pool papers say "we replicate…" in their abstract; the
gate never saw it because OpenAlex holds no abstract for them (Europe PMC had 86% of
the missing text). Lukas's intent: **Stage 1 runs every cheap abstract source with
generous or no limits; Stage 2's backfill runs only the restricted ones.** Check what
`search/fetch_abstracts.py` phases and `filter/engine/backfill.py` currently do and
where each source runs. The design question is scale: the gate runs over the whole
snapshot, and the no-abstract works in it are tens of millions — scope the pre-gate
fetch (e.g. works whose title passes a looser gate, or disciplines in scope) and cost it
before running.

### 8. The `curated-observatory` rule: find a general route, keep this one as a monitor

The spec says delete it once its works are extracted (they are). Lukas's refinement:
first check whether an existing or new *general* rule would route most of those 2,396
works into `screen_expensive` anyway. Set `curated-observatory` to `shadow: true`,
route, and count how many of its works still reach `screen_expensive` through other
rules and which rules those are (`filter.engine diagnose`). If a general rule captures
most, promote it for the next release and keep `curated-observatory` as a shadow rule
that measures retrieval against the Observatory's list. A shadow/delete change mints a
new release — do it before the next real `route`.

### 9. DOI twins (issue #210) — what it means

OpenAlex sometimes attaches a real replication's DOI to an unrelated record — e.g. a
1948 cosmic-ray paper carrying a JEAB 2010 DOI. Our pool joins on DOI, so the curated
rule admitted the WRONG record: the screen read the unrelated paper's text (or passed it
on the DOI's reputation), and extraction fused two papers. 30 were found among the
curated rule's admissions (`analysis/mo_observatory/doi_title_mismatch.csv`); the 27
still in the worklist are held out only by the `--only` file. Needed: (a) a Stage 2
discard rule that fires when the OpenAlex record's title/year disagree with the DOI's
registry (Crossref) record; (b) the same audit over the whole pool, since only the
curated admissions were checked.

### 10. Send Dan the email, with the DOI-issue sheet

The email is below (numbers checked 2026-09-23). Attach the Observatory's own data
issues as a **Google Sheet** shared by link (tabular and filterable beats a doc; `gws`
works with `GOOGLE_WORKSPACE_CLI_CREDENTIALS_FILE=~/.config/gws/gws_creds.json`). Rows
from `doi_pairs.csv` where the Observatory's side is the mistake — erratum, author
response, "Faculty Opinions" record, journal-issue DOI, unresolvable/truncated DOI, a
DOI for another paper, a Semantic Scholar URL in a DOI field — plus one note that its
per-effect/per-lab rows mean "one row per paper" comparisons must aggregate. Lukas
reviews before sending.

### 11. Low priority: Observatory rows without a replication DOI

262 rows / 188 works; 97 are OSF works, 93 of which are already in FLoRA (via FReD);
the rest are web.archive.org, datacolada, thesis-repository links. Only relevant if we
import from the Observatory. Document, don't build.

### 12. Report

`adjudication/report.html` covers the 169 pipeline items but not the 26 FLoRA items
(`verdicts("key_flora.csv", "codex6")`), and its intro prose predates the corrected
numbers. It stays local until both are updated; then publish with the comment layer
(`deploy-html`). `decisions.html` is the current per-paper view.

### Already done / decided (2026-09-23)

- FLoRA data-owner sheet **sent** by Lukas:
  <https://docs.google.com/spreadsheets/d/1INwfwx4WjInpqxgGm32H4C92CSWqt4opoMlcCkOvLYY/edit>
  (24 corrections, `flora_corrections.py`).
- `flor-011` → **keep FLoRA's** Duff et al. 2006 (*Nat Neurosci*, `10.1038/nn1601`): the
  paper says it "replicated the barrier protocol from our previous work (Duff, Hengst,
  Tranel, & Cohen, 2006)"; the 2011 definite-reference paper was in press and used as a
  comparison. `flor-023` → **keep** Asch 1956; the 1955 papers are optional extra
  originals, not an error. No follow-up to the data owner is needed.
- Outcome disagreements "success vs inconclusive" are definitional — see Context; raise
  with Dan, no pipeline change yet.

---

## Context

### The campaign (2026-09-21 → 22)

5,042 Observatory replication DOIs (4,855 distinct), each with one original per ROW —
but the file stores **one row per effect or lab** (the ego-depletion multi-lab study has
24 rows: 22 failure, 1 success, 1 reversal). Against FLoRA (1,837 replications with a
DOI): 1,378 in both, 459 FLoRA-only. The Observatory imports FLoRA (494 of the shared
works are FLoRA imports).

| Step | Result |
| --- | --- |
| Rule `curated-observatory` (release `c048ab6483d3`) | `screen_expensive` +1,345 |
| Screen | 1,469 works, 1,346 proceed (92%), ≈$2.3 |
| Extract (`--only analysis/mo_observatory/extract_only_2026-09-21.txt`) | 1,198 / 1,198; ≈$4.1 gpt-5.6-luna |
| Export `61aaeb9` | `extracted.csv` 3,040 → 4,141 rows, 908 new works |

The 710 out-of-pool Observatory DOIs were screened offline (`scope_differences.md`):
308 of 573 proceed. The 265 discards are scope differences (FLoRA requires a stated aim
to re-test; the Observatory counts designs), and 147 are the recall gap in step 7.

Fixed on the way: prompt hashes moved under Python 3.12 (`588f31a`, `_FROZEN_VERSIONS`);
the screen dry run priced the whole pile (`588f31a`).

### Agreement, corrected (supersedes the 2026-09-22 numbers)

`in_pool_agreement.py` joined against `priority.csv`, which kept one arbitrary
Observatory row per paper: 214 of the 1,303 works list more than one original there, and
115 original pairs carry more than one result. Recomputed per work from the raw file
(`agreement.py`):

| | |
| --- | --- |
| works both cover | 1,303 |
| same original (≥1 in common) | **87%** (1,136) — was reported as 84% |
| different original | 151 — of which 46 are the same paper under another DOI, so **105 genuinely differ** |
| same outcome, where both give one comparable verdict | **87%** (904 / 1,044) — was reported as 74% |

Not comparable: 84 originals where the Observatory records several results (per
effect/lab), 80 reproductions on our two axes, 71 where we gave no verdict. The
ego-depletion "contradiction" in the old draft was the dedup artefact, not an
Observatory error.

**Calibration** (`mo_vs_flora.py`): on 923 works FLoRA holds and the Observatory did NOT
import from FLoRA, the Observatory names the same original for 95% and the same outcome
for 75%. Outcome disagreement of our size is normal between careful sources.

### Blinded adjudication (`build_report.py`, `verdicts.csv`)

Two judges per item, answers labelled A/B at random, packet = the paper (full text
where cached, else looked up online): Claude (Opus 5.5) + Codex `gpt-5.6-sol`.

| Stratum (pipeline vs Observatory) | n | Observatory right | ours right | both | split |
| --- | ---: | ---: | ---: | ---: | ---: |
| different original (all) | 105 | 67 | 5 | 10 | 23 |
| success vs failure | 15 | 11 | 3 | — | 1 |
| we said cannot_be_determined | 19 | 14 | 1 | — | 4 |
| success/failure vs inconclusive | 30 | 13 | 6 | — | 11 |

Checks: first-shown answer chosen 43% (Claude) / 51% (Codex) when decisive — no
position bias; judges agree on 76%. 24 of the 26 wrong originals in the first 40 came
from `llm_references`, all at `link_confidence` high.

**Scope:** drawn from disagreements only. Estimated wrong-original rate on
`llm_references` rows ≈ 5–8% (disagreement with the Observatory 9–13%, most judged in
its favour). Rows where both databases are wrong are not measured.

### Why the confirmation check misses them

`_confirm_keyed_row` → `confirm_keyed_original` (`shared/prompts.py`,
`_KEYED_CONFIRM_TEMPLATE`): study title/abstract, the picker's own evidence quote, ONE
record; "say no only when the record is about something else, or by somebody else". The
quote usually does not name the original, and a same-author sibling on the same topic
passes by design. Example (`@schretlen2013` vs `@schretlen2014`, quote "we first
replicated earlier findings of reduced … grey matter and white matter volumes"): the
model picked 2014 `match_certain`; its reasoning never mentions 2013.

Cached prompts: `cache/llm/targetoutcome_*.json` store `llm_prompt` and `targets`, which
is what `replay_pick.py` replays.

### The success / inconclusive question (Lukas's item 10)

We never code a partial replication as success or inconclusive — FLoRA has `mixed`. The
Observatory has **no** `mixed`: its four values are success / failure / inconclusive /
reversal, so for comparison `mixed` and `statistically successful but flawed` map to
`inconclusive` (`TO_MO`). The 125 disagreements are therefore two different things:

| Ours → theirs | pairs | What it is |
| --- | ---: | --- |
| successful → inconclusive | 63 | The Observatory uses "inconclusive" for ambiguous/underpowered results too; judges split (8 split, 5 theirs, 4 ours) — a genuine boundary. |
| mixed → success | 24 | They call a replication "success" when the main effect replicated and a secondary did not; judges sided with them 5 of 5 sampled. Our `mixed` may be too eager — check the prompt's definition of mixed vs successful. |
| mixed → failure | 16 | Mirror case. |
| failed → inconclusive | 10 | Underpowered nulls. |
| flawed → success / failure | 12 | They have no "flawed". |

So: agree semantics with Dan ("main finding replicated, secondary not" — success or
mixed?), and check our own `mixed` threshold.

### Supabase status (read 2026-09-23; `SUPABASE_URL` set)

`shared/supabase_client.py` reads `SUPABASE_URL` at import time but does not load
`.env` itself — import `shared.config` first in any script. Tables: `unvalidated`
(3,706 records; `validation_status` ∈ unvalidated 2,959 · validated 351 · need_review
187 · rejected 86 · validation_inprogress 64 · consensus_reached 59), `validated` (349),
`validation_queue`, `record_metadata`. Matched on `doi_r` (`pair_id` also matches: stable
across the campaign export):

| `extracted.csv` works | not imported | queued, unvalidated | in progress / consensus / review | validated |
| --- | ---: | ---: | ---: | ---: |
| all | 938 | 2,014 | ~62 | 44 |
| `llm_references` (1,620) | 599 | 974 | ~27 | 17 |
| the 67 judged-wrong | 54 | 13 | 0 | 0 |

### FLoRA vs the Observatory (49 shipped-FLoRA works differ)

26 different paper (judged by Claude + Codex `gpt-6-sol`): Observatory right 14, FLoRA
right 6, both 3, split 3 (two of which are version issues). The 14 are the same
sibling-pick pattern as ours — human validation is not catching it either. 16
same-title pairs: 4 mistakes (all the Observatory's), 10 alternative identifiers (3 on
FLoRA's side), 2 formatting (FLoRA's `//` and `%3c`). 7 have no DOI on one side. All of
it is in the sheet sent to the data owner.

### Numbers for the email to Dan (2026-09-23)

- FLoRA replications not in the Observatory: 459 with a DOI + ~70 OSF-only (OSF guid not
  in the Observatory) ≈ **530**.
- Screened + extracted by us, in neither database: 1,620 with a DOI + 263 OSF ≈
  **1,900**, awaiting validation (~280 more rows carry other identifiers, unmatched).
- From his list: **908** new works extracted into our validation pipeline.

### Email to Dan (final draft — Lukas sends)

> **Subject:** Observatory × FLoRA: first exploration, and a chat in October?
>
> Dear Dan,
>
> Thanks again for sharing the Observatory database. We've done some exploration, and it
> looks like we each have a lot to add to the other's database:
>
> - FLoRA holds about 530 replications that aren't in the Observatory yet.
> - Our automated pipeline has also screened and extracted about 1,900 more replications
>   that aren't in either database yet. They're currently waiting for human validation.
> - In the other direction, your list led us to around 900 papers that we've now
>   extracted and queued for validation.
>
> The comparison also turned up some differences between our two pipelines, and some
> real strengths in yours. In particular, when we disagree about which original study a
> replication targets, your answer is usually the right one, and I'd like to learn how
> you do that.
>
> Could we chat in October about how to do this in practice, and how to make the
> collaboration visible and useful for everyone involved?
>
> Best wishes,
> Lukas

(+ link to the DOI-issue sheet from step 10, if ready.)

### Files

- `analysis/mo_observatory/adjudication/` — scripts, `decisions.csv/html` (193 works,
  one recommended action each), `verdicts.csv`, `doi_pairs.csv`, `flora_corrections.csv`,
  `replay_gpt-6-luna.csv`, `report.html` (pipeline items only).
- `analysis/mo_observatory/scope_differences.md`, `not_in_pool_report.md` — the
  out-of-pool analysis.
- Superseded: `handover-observatory-screen.md`'s sequence (its *Environment* section is
  still the reference for pool/cache gotchas).
