# Stage 3 resolution quality — iteration log

The exercise specified in [`docs/stage3-quality-handover.md`](../../docs/stage3-quality-handover.md):
iterate on a frozen 100-work development sample until three consecutive changes fail
to improve it, then confirm once on a disjoint 100-work holdout.

**Samples.** `samples.json`, drawn once from release `bc38ddd787e0` (1,298 works) with
seed 20260807. Dev 36/34/30 and holdout 34/35/30 (ordinary journal DOI / OSF
registration `10.17605` / URL-only) against the worklist's 36/33/30 — the simple random
draw reproduced the mix, so no redraw. The holdout is not run or read until the
stopping rule fires.

**Metrics**, per §1.3 of the handover. Every work gets exactly one label, assigned by
reading its stored payload:

| Label | Meaning |
| ----- | ------- |
| `correct_settle` | Settled, and the verdict is right |
| `wrong_settle` | Settled, but the original is findable from what the row records, or `doi_o` is the wrong paper |
| `correct_open` | `target_pending` / `api_error`, and there was nothing more to do |
| `missed` | Left open, but the row's own evidence names a findable original |

- **Wrong-settle rate** = `wrong_settle / 100`. Primary: the only error the pipeline
  cannot recover from, because `no_original_found` / `resolved` / `provisional` /
  `not_a_replication` close a work permanently.
- **Resolution yield** = `correct_settle with a link written / 100`. Secondary.

**Stopping rule.** Stop when three consecutive changes each fail to cut wrong-settle by
≥ 2/100 or raise yield by ≥ 3/100. Then run holdout once; gains generalise if holdout
wrong-settle is within 5 points of dev's.

**Budget.** $20 approved by the maintainer on 2026-08-07. Actual spend per iteration is
the `cache/token_usage.json` delta, taken by `python -m analysis.stage3_eval.spend`.

---

## Results

| # | Change | wrong_settle | correct_settle | missed | correct_open | yield | spend |
| - | ------ | -----------: | -------------: | -----: | -----------: | ----: | ----: |
| 0 | baseline (commit `b4f6f2f`) | | | | | | |

---

## Candidate changes, and why each is justified mechanically

Only changes justifiable without reference to their effect on the dev sample are
eligible — that is the defence against fitting the sample. Sources: a codex review of
`e03fa2a..HEAD` run 2026-08-07, and the handover's §3.

1. **The pre-PDF title-search rung is the unimproved copy of a resolver that was
   improved.** `_search_title_for_original()` (`extract/link_original.py:551`) searches
   the RAW target description, takes the first provider's hit and treats a provider
   outage as "no hit" — then SETTLES the work through `_exit_resolved`.
   `title_search_candidates()` (`:618`), which the lower rung uses, strips the citation
   prefix, keeps both providers' hits and reports an outage separately. The code's own
   measurement, in the comment at `:1172`: the raw string resolved 2 of 4 real campaign
   targets, the stripped title 4 of 4. Two implementations of one question, and the
   settling rung is the worse one.
2. **An unavailable OpenAlex candidate lookup is erased.** `find_all_candidates()`
   returns `None` for "did not answer", and `run_for_doi()` turns it into `[]`
   (`extract/link_original.py:855`), after which an LLM `llm_no_target` settles
   `no_original_found` — "nothing exists" written over "a source never answered".
   Directly against CLAUDE.md's rule that a swallowed error must never become an empty
   result.
3. **One resolved target closes a work whose other target errored.** `_verdict_for()`
   (`extract/tier.py:294`) ranks `resolved` above `api_error`, so a two-target work
   where one target's search hit an outage settles, and the second target never
   reopens.
4. **Author-and-year targets with no title.** The handover's §3: "Conceptual
   replication of Hyman & Sheatsley (1950) Study 2". Issue #186's route — an OpenAlex
   author+year filter query (1x, not the 10x of a free-text search) followed by an LLM
   plausibility pass over the candidates. Known negative result, not to be repeated:
   `resolve_doi_by_metadata()` scores by title similarity and resolved none of them.
5. **Which of two disagreeing candidates is the original is decided by list order.**
   `_title_searched_entry()` takes `candidates[0]` (`extract/run_extract.py:983`),
   which is CrossRef whenever CrossRef answered. CrossRef and OpenAlex returned
   different originals for 2 of 4 real targets.

### Corrected in review

The codex review reported that `_title_searched_entry()` rejects author-and-year
descriptions as `unsearchable`. It does not: `usable_title()` only rejects strings
under 10 normalised characters or recognised citation fragments, and "Zhong et al.
(2010)" passes both. Such a string IS searched — fruitlessly, at 10x a filter query.
The `unsearchable` outcome fires for fragments like "Study 2".
