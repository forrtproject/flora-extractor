# `mo_observatory/` — the Metascience Observatory comparison

**Throwaway.** Everything here is about works that are NOT in the survivor pool and are
not going into it. Nothing under this directory writes to `data/`, the routing store,
the verdict store or the pool; the two scripts call the pipeline's own functions so the
answers are the pipeline's, but no artifact of the pipeline is touched.

Ordinary Stage 2 work on Observatory DOIs that ARE in the pool lives one level up, in
`analysis/mo_observatory.py` (the priority list and the candidate `doi_in` rule).

## What was measured

Of the Observatory's 4,855 distinct replication DOIs:

```
4,855
├─   710  not in the survivor pool     <- everything in THIS directory
└─ 4,145  in the pool
   ├─ 1,776  already FLoRA / our exclusions
   └─ 2,369  new to us (579 already past Stage 2)
```

The 710 split by cause in `not_in_pool_report.md`: 651 are search-gate misses (the work
is in OpenAlex, its title and abstract carry no replication stem), 43 sit in the pool
under a sibling record, 6 are join artifacts from malformed Observatory DOIs, and 11 are
genuine coverage gaps. 78 of the 651 are already ours, leaving **573**.

## The files

| File | What it is |
| --- | --- |
| `not_in_pool_report.md` / `not_in_pool_causes.csv` | Why each of the 710 is missing, per work. |
| `screen_offline.py` → `screened.csv` | All 573 through `classify_replication()` — shipped prompt, shipped voters, shipped cache keys. |
| `curated_candidates.csv` | **The deliverable**: 214 works the screen calls replications or reproductions. |
| `extract_offline.py` → `extracted_offline.csv` | Those 214 through `_process_row()` — the real Stage 3 ladder. |
| `curated-observatory.json`, `priority.csv` | The Stage 2 candidate rule; about the pool works, not these. |
| `observatory_doi_issues.py` → `observatory_doi_issues.csv` | The Observatory's own DOI problems (erratum/notice, issue DOI, unresolvable, another paper, S2 URLs, malformed), from `adjudication/doi_pairs.csv` plus a Crossref scan of every DOI in the export (`--fetch`, cached in `adjudication/out/`). |
| `observatory_doi_sheet.py` | Writes that table + a notes tab to a private Google Sheet via `gws` (id in `observatory_doi_sheet_id.txt`; never shares it). |
| `no_replication_doi.py` / `.md` | The 262 rows / 188 works whose replication has no DOI, by kind (handover step 11). |

## Three things to know before reading the numbers

**Abstract recovery changed the population.** Europe PMC supplied 303 abstracts for works
OpenAlex had none for. An earlier pass concluded "the abstracts don't exist" from
CrossRef alone, which does return 0 here. Scopus is configured but answered HTTP 401:
Elsevier entitlement is IP-bound, so it needs the subscribing network or
`ELSEVIER_INSTTOKEN`, and the phase stops cleanly rather than recording false misses.

**One voter settles a proceed but not a discard.** `OPENAI_API_KEY` is unset, so
`SCREENING_MODEL_2` never ran. `screen_gate()` discards only on unanimity, so a work
whose first voter said anything but `none` is settled as proceed — that is the 214. The
284 works where voter 1 said `none` are genuinely undecided and need the second vote.
Votes cache per-vote, so finishing costs only the missing voter.

**`extracted_offline.csv` currently holds no real outcomes.** Both `LINKING_MODEL` and
`OUTCOME_MODEL` used `gpt-5.6-luna` at the time, which routes to OpenAI direct with no
fallback, so the run was `--no-llm`: the deterministic rungs only. Two consequences
are visible in the file:

* 21 of the 22 links it found are `single_candidate_after_requery` or
  `same_author_year_title_overlap` — the two `_HELD_ONLY_METHODS`, which a real run
  withholds because neither carries a semantic check. `--no-llm` never withholds.
* 13 rows carry an `outcome_keyword_guess` instead of an `outcome`. `--no-llm` falls
  back to `_keyword_scan`, which coded "We consider young children's construals of
  biological phenomena" as `failed` at `outcome_confidence: high`. Those are quarantined
  rather than shipped as outcomes.

So the deterministic pass recovers nothing trustworthy on this population, which is
itself the finding: these works need the model for both halves.
