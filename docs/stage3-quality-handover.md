# Stage 3 quality handover — 2026-08-07

A working document for a fresh session. Delete it once the goal below is met.

Read [`CLAUDE.md`](../CLAUDE.md) first, especially **"Before a Run That Spends"** — its
three rules were written after both were broken during the session that produced this
file, and following them is not optional here.

---

## 1. The goal

**Iterate on Stage 3's resolution behaviour until no further change measurably improves
it on a 100-work development sample, and confirm on a disjoint 100-work holdout that
the gains are real rather than fitted to the development sample.**

Stage 3 currently gets a large fraction of works wrong in a way that is expensive and
irreversible: it CLOSES a work with a settling verdict (`no_original_found`,
`resolved`, `provisional`, `not_a_replication`) when the right answer was either a
different original or "come back later". Only `target_pending` and `api_error` reopen.
The whole campaign is 1,314 works; getting the closing rule right before spending on
all of them is the point of this exercise.

### 1.1 The two samples

Draw both from the worklist of release `bc38ddd787e0`, once, and freeze them to disk:

```bash
.venv/bin/python - <<'PY'
import json, random
# ... build `works` exactly as scratch_worklist_probe.py does ...
ids = sorted(w.work_id for w in works)          # deterministic order
random.Random(20260807).shuffle(ids)            # fixed seed, recorded here
json.dump({"dev": ids[:100], "holdout": ids[100:200]},
          open("analysis/stage3_eval/samples.json", "w"), indent=2)
PY
```

Requirements, all of them load-bearing:

- **Disjoint.** No work in both.
- **Frozen.** Written to `analysis/stage3_eval/samples.json` and committed. The
  worklist shrinks as works settle, so a sample redrawn later is a different sample
  and every earlier number becomes incomparable.
- **Representative of the real mix**, which is 35% ordinary journal DOI, 33% OSF
  registration (`10.17605`), 30% URL-only, 1% neither. A simple random draw gives this;
  check it and record the actual composition of each sample alongside the ids.
- **The holdout is not looked at while iterating.** Not once. Reading it converts it
  into a second development sample and the exercise loses its only defence against
  overfitting.

### 1.2 How to run a sample

Always in the sandbox, which records real verdicts the live export ignores:

```bash
.venv/bin/python -m extract.tier --run --release bc38ddd787e0 \
    --mode validation --only "$(paste -sd, dev_ids.txt)" \
    --batch-label eval-dev-<iteration>
```

`--mode validation` verdicts do not settle the live worklist, so a work can be re-run
freely. Re-running IS the promotion when the time comes.

### 1.3 The metrics

Adjudicate every work in the sample by reading its stored payload — the verdict, the
`link_evidence`, the recorded title-search attempts, and `doi_o`. Assign exactly one
label:

| Label | Meaning |
| ----- | ------- |
| `correct_settle` | Settled, and the verdict is right (right original, or genuinely none findable) |
| `wrong_settle` | Settled, but the original is findable from what the row itself records, or `doi_o` points at the wrong paper |
| `correct_open` | `target_pending` / `api_error`, and there was genuinely nothing more to do |
| `missed` | Left open, but the row's own evidence names a findable original |

Two numbers, per sample:

- **Wrong-settle rate** = `wrong_settle / 100`. **This is the primary metric.** It
  measures the only error the pipeline cannot recover from.
- **Resolution yield** = `(correct_settle where a link was written) / 100`. Secondary:
  a pipeline that settles nothing has a wrong-settle rate of zero and is useless.

Record both, per iteration, in `analysis/stage3_eval/REPORT.md` — one row per
iteration, with the change that was made. Follow the shape of
`analysis/screening_eval/report_v33.md`, which is the project's precedent for this.

### 1.4 The stopping rule

Iterate on DEV. Stop when **three consecutive changes** each fail to do either of:

- reduce wrong-settle rate by ≥ 2 works in 100, or
- raise resolution yield by ≥ 3 works in 100.

Then, and only then, run HOLDOUT **once**.

- If holdout wrong-settle rate is within **5 points** of dev's, the gains generalise.
  Stop; the goal is met; proceed to the production campaign.
- If holdout is worse by more than 5 points, the last changes were fitted to dev.
  Say so plainly in the report, revert the changes that cannot be justified
  mechanically (as opposed to by their effect on dev), and re-specify before drawing a
  new holdout. **Do not reuse the burnt holdout** — draw a fresh 100 from the works
  used in neither sample.

### 1.5 Cost

A 100-work sandbox run is roughly **$1.50–2.00** in LLM spend at the current full-text
reach (~60% of works acquire a document; measured over 25 works on 2026-08-07), plus
about 700 OpenAlex credits, plus 10× credits for each title search. Each iteration
costs about that much again, because changing acquisition changes the prompt inputs and
therefore misses the LLM cache.

**Budget the whole exercise at roughly $20–30 and confirm that with the maintainer
before starting.** Per `CLAUDE.md`'s API-cost rule, state the estimate up front.

**Token accounting is currently broken** — `cache/token_usage.json` records input 0 /
output 0 for every model on 2026-08-06 and 2026-08-07, so actual spend cannot be read
back. Fix or work around this early, or the cost numbers in the report will be guesses.

---

## 2. Where things stand

Branch `fix/stage3-campaign-blockers`, six commits on top of `e03fa2a`. **Not pushed,
no PR.** 1,394 tests pass.

```
1d87f19  The real cause of the OpenAlex failures, and three findings from the codex review
6f290b1  Two endings for a title search, and OpenAlex 5xx is retried rather than fatal
476d7fc  --redo stops re-admitting a work once it has been redone, and three rules before a paid run
37d49d5  A named-but-unmatched target is searched, not settled as no_original_found
95c48a7  Two structured document sources: OSF registrations, and HTML with a landing-page test
fb1eb7c  Three bugs the 2026-08-07 campaign hit, at 84 of 1,325 works
```

### 2.1 Uncommitted, and needs a test before it is committed

`extract/run_extract.py` carries an uncommitted change that records **every title
search attempt** — the string searched, the outcome (`resolved` / `no_match` /
`unavailable` / `unsearchable`), and the candidates — onto the work, including onto
`link["llm_evidence"]` so it survives when NO target resolves and the work is written
by the single-row path.

This exists because of a specific finding: for a work that settles `no_original_found`,
**nothing was stored about what had been tried**. The structured `target_as_named`, the
author and the year are not persisted anywhere, so evaluating a better resolver would
have required re-running every work rather than reading stored rows. It is a
prerequisite for the goal above — adjudication depends on it.

It has no test yet. Write one (the class `TestASearchThatFoundNothingIsRecorded` was
drafted and not saved) and commit before iterating.

### 2.2 What was fixed this session

| Bug | Evidence it was real |
| --- | --- |
| NUL byte in a payload aborted the whole run | HTTP 400 `22P05` from `engine_verdicts` killed a 1,325-work campaign at work 84 |
| Parse cache collided for DOI-less rows | `parse_d41d8cd9….json` on disk; 4 works logged byte-identical parses; 30% of the worklist has no DOI |
| `_cached_oa_xml` never hit | 0 of 285 rows matched the DOI-derived name; the writer files under the OpenAlex id |
| 63% of works could acquire no document | Added OSF registration (API) and HTML sources, each with a content check |
| A named-but-unmatched target settled as `no_original_found` | 15 works closed with the original named in plain text in their own evidence |
| OpenAlex "failures" | **A comma in a filter value is HTTP 400** — "Commas separate filters". Target descriptions are citations, so nearly all of them 400'd |
| `--redo` looped forever | The batch loop re-applied the redo set on every rebuild; 29 works re-extracted 9 times in 10 minutes |
| Partial outage still settled (codex P0) | `unavailable` was true only when NEITHER provider answered |
| HTML guard measured length, not scholarship (codex P1) | With no abstract detected it collapsed to "is this page 10,000 characters" |
| Transient source failure became a 14-day verdict (codex P1) | `_failed()` was called on timeouts as well as on real absences |

### 2.3 Measured effect so far

25-work sandbox run, versus the live pilot before these changes:

| | Before | After |
| --- | --- | --- |
| OpenAlex request failures | 15 per 27 works | 0 |
| Documents acquired | 15/25 | 17/25 |
| `resolved` | 0 | 1 |
| `provisional` with a real DOI | 1 | 7 |
| `no_original_found` | 15 | 10 |
| `target_pending` | 9 | 7 |

These are different works in each run (the worklist moved on), so treat the direction
as real and the magnitudes as indicative. **Establishing comparable numbers is exactly
what the frozen samples in §1.1 are for.**

---

## 3. The known-open problem, and one negative result

Of the 10 remaining `no_original_found` in the sandbox run, **4 name an original that a
search ought to find**:

```
6887769564  "Conceptual replication of Hyman & Sheatsley (1950) Study 2"
4412006237  "…Direct Replication of the Scaffolding Experimental Study by D. Wood et al. (1978)"
6887694362  "…the room brightness employed by Zhong et al."
```

They name an author and a year but **no title**, so a title search has nothing to
search on and `strip_citation_prefix` correctly declines to strip (it would leave
"Study 2").

**Negative result, verified — do not repeat it.** The obvious fix, an author+year
lookup through `resolve_doi_by_metadata()` in `shared/doi_verify.py`, was tested
against these exact strings and resolved **none** of them — including
"Toya and Skidmore (2007)", which the plain title search *does* resolve. That function
scores hits by title similarity and returns `None` on an empty title, so it cannot
serve this case. Building it would have been wasted work.

The path that should work is the one already opened as **issue #186**: generate
candidates (an OpenAlex author+year filter query is cheap — 1×, not the 10× a free-text
search costs) and have an LLM judge which, if any, is plausible. The maintainer's
framing: *"this obviously can't be trusted without the subsequent LLM pass over
candidates."* Two prompt shapes to compare are written up in the issue.

Note also that CrossRef and OpenAlex **disagree** on 2 of 4 real targets — for
"D. Wood et al. (1978)" CrossRef returns a book-chapter reprint and OpenAlex the 1976
paper. The current pick is naive (CrossRef first). Every candidate is recorded on the
row, which is the test data #186 needs.

---

## 4. State that needs cleaning

The `--redo` loop left **360 live result rows over 84 works**; 36 works have more than
one live result row and one has 12. `_decide()` takes the latest row per work, so every
decision and the export are correct — what is wrong is the documented invariant of one
result row per work per run. Supersede the stale duplicates in one pass before the
numbers in any report are read off row counts.

Separately, **29 works carry live settling verdicts** from the pre-fix runs
(`scratch_redo_ids.txt` holds the ids). Some were closed by bugs since fixed. Decide
whether to `--redo` them before or after the evaluation; they are not in either
frozen sample unless the draw happens to include them, which is another reason to
record the samples' composition.

---

## 5. Traps this session hit, so the next one does not

1. **`--redo` on a live run without reading the batch loop.** Cost: 9× redundant
   extraction. Read any worklist-changing flag before spending through it.
2. **A live pilot instead of `--mode validation`.** Cost: 15 works closed with wrong
   settling verdicts. The sandbox exists; use it.
3. **Exporting after a failed run.** `extract/export.py` renders the current generation
   WHOLE, so an export after a partial run replaced `data/extracted.csv`'s 285 rows
   with 6. The campaign script now gates the export on the tier succeeding
   (`scratch_stage3_campaign.sh`). The 285 rows predate the state authority — they were
   written by the retired CSV runner and have no verdict rows — so 143 of their 269
   DOIs get rebuilt by the campaign and 124 correctly drop out, being already in the
   Supabase validation tables.
4. **Assuming rather than probing.** Extending the OSF regex to `10.17605` looked like
   the obvious fix and would have done nothing: those DOIs are registrations, not
   files, and `osf.io/download/<guid>/` returns HTTP 500 for every one. Ten minutes of
   probing the API found the route that works.
5. **`_search_openalex_by_title` and friends are cached on content-complete keys.**
   Changing the query string (as the comma strip and the prefix strip both do) mints
   new keys, so old entries miss rather than mis-read. That is by design; do not
   "fix" it by loosening the key.

---

## 6. Commands

```bash
# the worklist and its composition
.venv/bin/python scratch_worklist_probe.py

# dry-run estimate (its rung reach is measured off the OLD extracted.csv and
# UNDERSTATES full-text cost by roughly 7× — see §1.5)
.venv/bin/python -m extract.tier --release bc38ddd787e0

# a sandbox run over named works
.venv/bin/python -m extract.tier --run --release bc38ddd787e0 --mode validation \
    --only <ids> --batch-label <label>

# read back what a batch decided
#   claims carry meta.batch (NOT meta.batch_label) and meta.mode
#   import shared.config FIRST or ClaimsClient raises ClaimsNotConfigured
```

Scratch files at the repo root (`scratch_*`) are this session's and can be deleted:
`scratch_worklist_probe.py` is worth keeping until the samples are drawn.
