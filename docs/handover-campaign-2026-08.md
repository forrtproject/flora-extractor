# Handover: the 2026-08 lockdown campaign

The runbook for working through the open entries in `PENDING_RUNS.md`. That file
holds the pasteable commands and done-criteria; this brief holds the ORDER, the
gates between steps, and what was changed on 2026-08-13 that the runs now depend on
(commits `d9f979d` — the pre-spend review's fixes — and `3fb6efe` — the OSF
aliases). After the campaign the pipeline is frozen except the rule set.

## The order, and why it is fixed

1. **Reopen the 55 shared-guid works** (issue #201; the `drop_misses` one-liner).
   Cheap, unblocks the backfill from re-recording the contaminated answer.
2. **Re-run the OSF projects backfill** (issue #196, the Done entry's commands).
   Gate first: `https://api.osf.io/v2/nodes/7weum/` must answer 200 — the first run
   died on OSF-side 500s under our own load; consider `OSF_RATE_SEC=2`. The nodes
   arm now stops on a credential refusal and skips rows that already have pool text.
3. **Freeze and re-route, ONCE, carrying all three waiting changes**: the frozen
   overlay chunk, the `no_text` exemption, and the issue-#200 aliases (already in
   `filter/spec/aliases.json`). Each route mints a release; a screen run between two
   routes pays twice for works about to be merged. Expect `pending` down ~849 and
   the duplicate groups collapsed. **Record the new release id** — every later
   command names it; `56076eb` and `8b3d` both refuse after this route.
4. **Smoke test before the screen spend**:
   `screen --tier screen_expensive --run --limit 20 --mode validation --release <R>`.
   It must show two votes per work (no `None`), sane per-call wall clock, and a
   `served_by` host on the DeepSeek votes. This is the live check that OpenRouter's
   `require_parameters` + throughput-floor routing leaves an eligible host; if votes
   come back missing, the routing block in `call_openrouter` is the first suspect.
5. **Re-screen** (~$1.80 DeepSeek for ~10k works; gpt-5.4-mini side is cache reads,
   including the model-less joint-era entries, lifted by provider). Off-peak halves
   the DeepSeek price from 2026-08-16.
6. **Between screen and extract, two free reads** (both were re-measured nowhere —
   the voter swap changed them and the eval did not cover them):
   - diff `screen_record_type` old vs new on the ~102 works where the old pair split
     replication/reproduction — voter 1 is the tie-breaker and it changed; the field
     picks the outcome vocabulary.
   - count both-qualifying-AND-confident works vs the old 6,551/10,456 — that share
     gates the `llm_title_search` rung, i.e. the 10×-priced OpenAlex searches.
7. **Pre-extract purges and pulls** (in the #198 entry): `analysis.purge_osf_docs
   --apply`, `analysis.purge_epmc_retries --apply`, `cache_sync --pull --parts
   doi_verify`. Both purges print a dry run without `--apply`.
8. **Re-extract + export** against the step-3 release. Expect ~30 min of local
   re-parse first (`TEXT_EXTRACTION_VERSION` moved to 2). Done-criterion: the
   export prints NO `rows from a superseded generation:` line (`--check` does not
   test carry-forward), `print_search_summary()` reports ≈0 verification searches,
   and the row count falls by ≈165 (the alias merges) plus the 342 re-routed
   preregistrations, minus rows the newly-recovered OSF text adds.

## What changed under the runs (read before debugging one)

- **Screen**: voter 1 is `deepseek/deepseek-v4-flash@low`, gate G-unanimous, one
  cache entry per VOTE. The screening generation names each voter's effort. The
  serving host is stored on OpenRouter votes as provenance.
- **Extract evidence**: multi-target works descend past the pre-PDF title search
  into acquisition, and their full-text call sends the whole parsed body (60k-char
  cap). The outcome rules are scoped per target; a collective verdict covers its
  set; an unattributed count covers nothing. The author-year pick answers with
  `@surname2010` keys.
- **Acquisition**: Europe PMC is one tier, JATS `fullTextXML` first (`epmc_xml`, a
  structured source), rendered PDF on 404; transient failures no longer record its
  14-day suppression. Empty reference lists are cached as answers.
- **Guards**: the export refuses a stale release like the tier; a worklist parquet
  without `has_text` is refused; `--limit` bounds the pool scan again;
  `doi_duplicates --apply` merges into `aliases.json` instead of rewriting it.

## After the campaign

- Re-read section 2 of `handover.html` off the new render (the #198 entry says so).
- **Read the collected `search_confirm` grades** and decide whether the negative
  grades should gate anything (`analysis/stage3_eval/search_confirm_plan.md`).
- **Test-suite cleanup pass** (maintainer-requested): the suite is ~1,770 tests and
  100–440 s; consolidate where one seam is pinned several times. Deferred until the
  freeze-and-verify cycle is over so frozen code is not touched twice.
- Two known stale prompt lines, deliberately left because touching them re-buys
  answers — fix only when their prompt next moves for a real reason:
  - `_CLASSIFY_PROMPT` STAKES says a confident "none" permanently discards; under
    G-unanimous it does not.
  - `build_keyed_confirm_prompt`'s closing "nothing is removed on your answer
    alone" understates that a confident "false" quarantines the row.
- Optional acquisition follow-up, evidence in the session record: an Elsevier
  ScienceDirect API tier would recover ~4–7 more paywalled-class works
  (`ELSEVIER_API_KEY` is already in `.env`); 17 document-less multi-target papers
  have no OA route at all and stay unrecoverable.
- The 405 alias-excluded groups can mostly be recovered by checking each group's
  titles against the guid's own OSF title history (one free API call per group —
  validated on all 51 sampled during adjudication). Worth doing only if the
  excluded 678 works matter to coverage.

## Where the evidence lives

- The review, decisions and their dispositions: `scratch_prespend_lockdown_review
  .html` (local page, comment threads resolved) — the review that produced
  `d9f979d`.
- Screen voter/gate eval: `analysis/screening_eval/cheap_voter_2026-08.md`.
- Alias derivation, adjudication and QC: the `3fb6efe` commit message;
  `docs/handover-osf-dedup.md` for the original brief and canonical rule.
- The `about_replication` discard audit (0 false discards; pooling meta-analyses
  are out of scope by maintainer ruling): session record, 2026-08-13.
