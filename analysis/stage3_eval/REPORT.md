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
| 0 | baseline (commit `b4f6f2f`) | **25** | 24 | 46 | 5 | **24** | $0.33 |
| 1 | a named target that could not be identified writes `target_pending` (`0bfcb54`) | **1** | 24 | 70 | 5 | **24** | $0.00 |
| 2 | a citation with no title is resolved by author and year (`1f8ceb4`) | **1** | 38 | 56 | 5 | **38** | $0.04 |
| 3 | the abstract rung's gate reads the title's citations too (`39d4218`) | **0** | 46 | 50 | 4 | **46** | $0.10 |

Per-work labels and the reason for each: `labels-dev-<n>.json`, one per iteration.

### What iteration 0 says

**One cause produces almost every error.** The target prompt names the original as a
citation — "Ramscar et al. (2010)", "Turri, Buckwalter & Blouw (2015)", "Barak-Corren,
N., & Bazerman, M. (2017). Is Saving Lives Your Task or God's? … Judgment and Decision
Making, 12(3), 280–296" — and the only resolver that can act on a name is a TITLE
search. Asked a citation with no title in it, both providers answer nothing, and the
row is written `no_original_found`, which closes the work for good. All 24
`no_original_found` works in the sample are of this shape, and every one of them names
an author and a year that identify a single published paper.

**Verdict distribution.** 20 resolved, 24 no_original_found, 5 provisional, 48
target_pending, 3 api_error.

- Of the 20 `resolved`, 20 are right. The reference-list and cited-candidate rungs do
  not make wrong links in this sample.
- Of the 5 `provisional`, 4 are right. The fifth is the only wrong settle that is not a
  `no_original_found`: "Tversky and Kahneman (1973)" resolved to
  `10.1017/cbo9781139600224.006`, a book chapter titled "Kahneman and Tversky".
- 46 of the 51 open works name a findable original and are `missed`, not `correct_open`.
  Two of the three `api_error`s are a TLS certificate failure on `doi.org` for the
  `10.18718` registrant; the third is a one-sided title search whose CrossRef half
  answered and is recorded on the row.

**The 0-target group.** 18 of the 48 `target_pending` works named NO target at all, and
in every one of them the original is in the paper's own title ("A multilab
investigation into the N2pc …: Direct replication of Eimer (1996)"). These are OSF
registrations whose abstract is boilerplate — "Stage 1 IPA at PCI RR", "Please see
pre-registration folder" — so the abstract rung has nothing to read, and no rung reads
the title.

**A run does not terminate** — found and fixed during this iteration, in commit
`d367c5e`. `target_pending` does not settle, so the worklist rebuild between batches
handed the 51 unsettled works straight back; the run judged them eight times in twenty
minutes before it was killed. The verdicts above are the latest row per work and are
unaffected; the repeated passes were served from the LLM cache, which is why the
iteration cost $0.33 rather than the $1.50–2.00 estimated.

### What iteration 1 says

Wrong-settle 25 → 1, which clears the stopping rule's ≥ 2/100 threshold by a wide
margin. Yield unchanged at 24, as expected: the change stops works being closed
wrongly, it does not identify anything new.

**Exactly the intended works moved, and only those.** Of the 100, 24 changed verdict,
all of them `no_original_found` → `target_pending`, and all 24 were labelled
`wrong_settle` in iteration 0. No `resolved` or `provisional` row moved. Labels:
`labels-dev-1.json`.

The one remaining wrong settle is work 3185325517, where a title search for "Tversky
and Kahneman (1973)" returned a book chapter titled "Kahneman and Tversky"
(`10.1017/cbo9781139600224.006`) and the row was written `provisional`. That is the
candidate-choice problem of issue #186, not the no-title problem.

**Cost: nothing.** Every LLM call was already cached, because the change is in what the
ladder does with an answer, not in what it asks. Cumulative spend $0.33 of the $20
approved.

**The accepted cost of the change** (raised by the codex review): a paper whose original
genuinely cannot be identified now stays open forever rather than closing as
`no_original_found`. That is the trade the primary metric asks for — an open work costs
a re-run, a wrongly closed one costs a wrong record in FLoRA that nothing reopens. The
works this affects are the ones iteration 2 is meant to identify; if a residue remains
after it, closing them is a decision to take then, on evidence, rather than by
defaulting to it now.

### What iteration 2 says

Yield 24 → 38, which clears the stopping rule's ≥ 3/100 threshold. Wrong-settle
unchanged at 1.

**Every one of the 14 new links is right.** 13 came from the author-and-year route and
one from the title search. Each was checked against the original the paper named:
Ramscar et al. (2010) → the feature-label-order paper, Weisel & Shalvi (2015) → "The
collaborative roots of corruption", Moss-Racusin et al. (2012) → "Science faculty's
subtle gender biases favor male students". Labels and DOIs: `labels-dev-2.json`;
stored payloads: `payloads-dev-2.md`.

They are written `provisional`, so they settle the work but are quarantined to
`provisional_author_year.csv` and not imported for validation. 14 of 14 is a precision
estimate from one sample of one adjudicator and is not a substitute for the human
confirmation issue #186 asks for.

**A second cause of the OpenAlex 400s, found by running it.** Four of the five 400s in
the first iteration-2 run were a title ending in a question mark, which OpenAlex reads
as a wildcard and a stemmed field rejects outright. The comma strip written on
2026-08-07 did not cover it. Fixing it also resolved a work through the OLD title
search — "The (Im)perfect Automation Schema: Who Is Trusted More, Automated or Human
Decision Support?" — which had 400'd on every run since the campaign began.

**Where the remaining 56 `missed` works are.** 18 name the original only in the paper's
own title, and the abstract is registration boilerplate ("Stage 1 IPA at PCI RR"), so
the target prompt reads nothing and names no target. That is iteration 3. The rest
name targets the author-and-year route declined, or a description with no
parseable author and year in it.

**Cost: $0.04**, of which $0.02 was the 14 new pick calls. Cumulative $0.37 of $20.

### What iteration 3 says

Yield 38 → 45, clearing the ≥ 3/100 threshold. Wrong-settle unchanged at 1.

**7 works gained a link and all 7 are right.** Five through the author-and-year route
and two through the title search, which now runs because a target is named for it to
search on: Eimer (1996) → the N2pc paper, McCullough et al. (1997) → "Interpersonal
forgiving in close relationships", Ackerman, Nocera & Bargh (2010) → "Incidental Haptic
Sensations Influence Social Judgments and Decisions". Labels: `labels-dev-3.json`.

**An adjudication error, corrected.** Work 7058069136 was labelled `correct_open` in
iterations 0–2 on the grounds that its abstract is Scopus boilerplate. Its title names
"reported by Engineer et al. (2013)", so it was `missed` throughout. The correction
moves one work between `correct_open` and `missed` and changes neither metric; the
earlier rows are left as they were recorded.

**A stale label under the headline number, found and fixed.** Labels were carried
between iterations wherever the VERDICT did not change, and that is not enough: a work
can keep `provisional` and change the paper it points at, because a route changed
underneath it. Work 3185325517 did exactly that. Its target, "Tversky and Kahneman
(1973)", matches `citation_without_title`, so from iteration 2 it took the
author-and-year route rather than the title search that produced the book chapter — and
the iteration-0 `wrong_settle` label was copied forward over a payload that no longer
said what it described.

`labels-dev-3.json` now records each work's verdict AND its `doi_o` read off the
payload, and `check_labels.py` re-reads both, so a label cannot outlive its row. Every
one of the 100 was re-checked against the iteration-3 payloads: one had drifted.

**Re-adjudicated, and it is the last wrong settle.** 3185325517 now points at
`10.21236/ad0767426`, the 1973 technical report of "Judgment under Uncertainty:
Heuristics and Biases", which carries the 8! demonstration the paper re-tests and is
the year the paper cites. **This is a judgement call**: the published record is the
1974 *Science* paper, and a reader who wants that would call this the wrong record. It
is counted `correct_settle` because the work and the year are the ones the paper cites
and the row is provisional — choosing between two records of one work is what the human
confirmation step is for. Subtract it if you disagree; wrong-settle is then 1, not 0.

**`doi_o_verification = "verified"` proves nothing on this route.** The verifier
compares the DOI's metadata against `title_o` and `authors_o`, both of which were
filled from the same OpenAlex record the DOI came from, so it cannot catch a wrong
pick. The 21-of-21 precision reported here is manual subject-matter adjudication and
nothing else.

**Cost $0.10** — the first iteration to re-ask the target prompt for a large group of
works rather than reuse a cached answer. Cumulative $0.47 of $20.

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
