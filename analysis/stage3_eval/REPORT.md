# Stage 3 resolution quality — iteration log

The exercise specified in `docs/stage3-quality-handover.md` (since deleted — see "The
goal this file was written for" below): iterate on a frozen 100-work development
sample until three consecutive changes fail to improve it, then confirm once on a
disjoint 100-work holdout.

> The `payloads-*.md` files and `campaign.sh` referred to below are no longer beside
> this report: they moved to `archive/analysis/stage3_eval/`. The payloads are
> regenerable with `analysis/stage3_eval/read_batch.py`.

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
| 4 | the target prompt says what identifies a target (`635f5c0`) | **4** | 54 | 38 | 4 | **54** | $0.43 |
| 5 | the title search is given the year the paper cited (`84a4751`) | **4** | 54 | 38 | 4 | **54** | $0.02 |
| 6 | a title hit must carry the author the citation named (`7b7f891`) | **2** | 54 | 40 | 4 | **54** | $0.00 |
| 7 | the author-and-year query ANDs every surname the citation named (`ca43cdb`) | **2** | 66 | 29 | 3 | **66** | $0.02 |
| 8 | the citation parenthesis may carry a venue; a dead browser tab ends its tier, not the row (`d8?`) | **2** | 69 | 26 | 3 | **69** | $0.01 |
| 9 | the shortlist narrowing adds candidates instead of replacing them (`4b53cb2`) | **2** | 69 | 26 | 3 | **69** | $0.04 |
| 10–12 | one pooled candidate list per target, its two reviews, and PsycEXTRA | **1** | 74 | 22 | 3 | **74** | $0.13 |
| 13 | the shortlist asked of CrossRef first — REPLACING OpenAlex's | **1** | 73 | 23 | 3 | **73** | $0.06 |
| 14 | CrossRef ends the search only when it identified the paper; otherwise it leads a pool | **1** | 79 | 17 | 3 | **79** | $0.02 |
| 15 | a title hit is accepted on containment, not only Jaccard | **1** | 79 | 17 | 3 | **79** | $0.01 |
| 16 | the four faults holdout 3 and the maintainer found, plus six from a final review | **1** | 80 | 16 | 3 | **80** | $0.01 |

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

### What iteration 4 says — a gain on the secondary metric bought with four
irreversible errors

Yield 46 → 54. Wrong-settle 0 → **4**. By the letter of the stopping rule this counts
as an improvement, because the rule accepts a change that raises yield by ≥ 3. By the
goal the rule serves it does not: wrong-settle is the primary metric precisely because
it is the error nothing reverses, and this change created four of them where there had
been none.

**The 16 works that gained a link: 12 right, 4 wrong.** Every DOI was checked against
Crossref metadata rather than from memory. The four wrong ones:

| Work | Named target | Linked to | What Crossref says it is |
| ---- | ------------ | --------- | ------------------------ |
| 4297998882 | Bem (2011) | `10.1016/0005-7967(65)90022-7` | Eysenck 1965, "Personality and social psychology" |
| 6905495176 | Svensson (AEJ: Macroeconomics, 2015) | `10.1257/mac.2.1.i` | the journal's 2010 front matter, no author |
| 6906572393 | Olivola & Shafir (2013) | `10.1037/e513702014-051` | a PsycEXTRA conference abstract by Olivola alone |
| 7099891304 | Hamlin & Wynn (2011) | `10.1073/pnas.1110306108` | Hamlin, Wynn & Bloom 2011 — the right authors and year, a different paper |

Three of the four are a TITLE search matching something that is not the paper, and in
two of them the hit's year is decades from the year the target string carries: 1965
against a cited 2011, 2010 against a cited 2015. The prompt change produced more
title-shaped target strings, and the title search had no year to check them against —
`title_search_candidates` is called with `year=""`, so the ±2-year test it already
implements never runs. That is iteration 5.

**Three works lost a link they had.** 6906510766 (Weisel & Shalvi 2015), 6924979033
(McCullough et al. 1997) and 7160689708 (De Neys et al. 2013) were resolved in
iterations 2 and 3 and are `target_pending` again: the re-asked target prompt named
something the route could not resolve. A prompt change moves works both ways, which is
the reason every settled work is re-checked against its payload each iteration rather
than having its label carried forward.

**Cost $0.43**, the largest so far — a new prompt version invalidates every target call.
Cumulative $0.90 of $20.

### What iteration 5 says — the first change that fails the stopping rule

Wrong-settle 4 → 4, yield 54 → 54. Neither threshold met. **Failed change 1 of 3.**

The year filter did what it was built to do and it was not enough. Both hits it
rejected were replaced by other wrong hits inside the two-year window:

| Work | Was | Now | What Crossref says it is |
| ---- | --- | --- | ------------------------ |
| 4297998882 "Bem (2011)" | Eysenck 1965 | `10.1093/oxfordhb/9780195398991.013.0001` | Snyder & Deaux 2012, "Personality and Social Psychology" |
| 6905495176 "Svensson (AEJ: Macroeconomics, 2015)" | 2010 front matter | `10.1257/mac.5.2.i` | the journal's 2013 front matter, no author at all |

The change is kept: filtering a search by the year the citation gives is right whether
or not it moves this sample, and it now rejects the class of hit that is decades out.
What it exposes is that the title search's remaining failures are not about the year.
Both surviving hits carry the wrong AUTHOR — one names Snyder and Deaux, the other
names nobody — while the citation names Bem and Svensson. That is iteration 6.

**Cost $0.02.** Cumulative $0.92 of $20.

### What iteration 6 says

Wrong-settle 4 → 2, meeting the ≥ 2/100 threshold. Yield unchanged at 54. The two
title-search errors iteration 5 exposed are gone: the Oxford Handbook chapter is
dropped because it does not carry Bem, and the journal front matter because it has no
author at all. Both works are `target_pending` again, so a better resolver is still
offered them.

**The guard's first run cost a correct link, and that is why it was re-run.**
`extract_author_year_patterns` returns a multi-author citation as one run-on token —
"Kaufmann, Weber, and Haisley (2013)" comes back as `kaufmann,weber,andhaisley` — and
matching that as a word never hits an author list, so the right Management Science
paper was dropped. The value is now split and any of its names carrying is enough.

**The two wrong settles that remain** are both author-and-year picks where the author
and the year are right and the paper is not: "Olivola & Shafir (2013)" →
a PsycEXTRA conference abstract by Olivola, and "Hamlin & Wynn (2011)" → the authors'
OTHER 2011 paper. Neither is reachable by a metadata rule; both are subject-matter
judgments the model made and got wrong.

**Cost: nothing** — no new question was asked, only answers filtered.
Cumulative $0.92 of $20.

### What iteration 7 says

Yield 54 → 66, the largest single gain of the exercise. Wrong-settle unchanged at 2.

**12 works gained a link and all 12 are right**, every DOI checked against Crossref:
Jones & Macken (1995) → "Organizational factors in the effect of irrelevant speech",
Khan & Dhar (2006) → "Licensing Effect in Consumer Choice", Fine, Jaeger, Farmer & Qian
(2013) → "Rapid Expectation Adaptation during Syntactic Comprehension". One of them
carries two originals, both right. Labels: `labels-dev-7.json`; payloads:
`payloads-dev-7.md`.

The gain is entirely selectivity. `extract_author_year_patterns` reports one surname
per citation, so "Jones and Macken (1995)" was searched as "jones 1995" — 8,348
OpenAlex works, with the right paper nowhere near the eight the model was shown. ANDed
with "macken" it is 7 works and the right paper is third. The names were always in the
citation; nothing was asked that had not been asked before.

**Running total: 66 of 100 works settled correctly, 2 wrongly, from 24 and 25 at
baseline.** Cumulative spend $0.94 of $20.

### Where the 29 remaining misses are, after iteration 7

Read off each work's recorded `_search_attempt`:

| Works | Recorded outcome | What they are |
| ----: | ---------------- | ------------- |
| 12 | `no_match` on a title search | 8 are still a description of the finding rather than a citation ("study of ambiguity aversion", "the information protocol condition"); 4 are citation-shaped strings the search did not match ("Wilson et al. (2017, JPSP", `"Bats, Balls, and Substitution Sensitivity"`) |
| 10 | `author_year_declined` | the model was shown a shortlist and said none of them. Includes "Turri, Buckwalter, Blouw (2015)", whose original a DIFFERENT work in this sample resolved — so the shortlist, not the target, is what failed |
| 4 | no target named at all | two are the same OSF registration of Vess (2012), one names its original only in a phrase the prompt reads as prose |
| 2 | `api_error` | a TLS certificate failure on `doi.org` for the `10.18718` registrant; these reopen |
| 1 | `unsearchable` | a target description with no author, year or title in it |

No bucket now carries an obvious single cause the way the first four iterations did.
The remaining candidates are small (a citation pattern that does not handle
"(2017, JPSP)") or are tuning knobs (how many candidates the shortlist offers), and a
tuning knob moved to fit 100 works is exactly what the holdout exists to catch.

### What iteration 8 says

Yield 66 → 69, meeting the ≥ 3/100 threshold. Wrong-settle unchanged at 2.

Three works gained a link, all Crossref-checked: the two OSF registrations of "Vess
(2012, PS, Study 1)" → "Warm Thoughts", and "Blau and Kahn (Journal of Economic
Literature 2017)" → "The Gender Wage Gap: Extent, Trends, and Explanations". The first
two came from the parenthesis fix — every citation pattern demanded the year alone, so
a parenthesis carrying the venue read as no citation at all. The third came from the
browser fix: the ladder reached its abstract rung for the first time, having previously
been aborted by a TLS certificate error two tiers below it.

The remaining `api_error` work is now `target_pending`, which is the honest ending for
a row whose document could not be fetched.

**Cost $0.01.** Cumulative $0.95 of $20.

### What iteration 9 says — net zero, and kept anyway

Wrong-settle 2 → 2, yield 69 → 69. Neither threshold met. **Failed change 2 of 3.**

Two works gained a correct link — Wilson et al. (2017, JPSP) → "Racial bias in
judgments of physical size and formidability", Zuckerman et al. (1993) →
"Contemporary Issues in the Analysis of Data" — and two lost one they had, Usta &
Häubl (2011) and Van Lange et al. (1997). A different shortlist led the model
somewhere else on those two.

**It is kept.** Each of its three parts is a defect independent of this sample: a
`.search` value ANDs every word, so narrowing on a whole title matched nothing and the
narrowing had never once run; a narrowing that replaces the broad list can hide the
right paper, and did; a citation's spelling and a record's differ by an accent. Undoing
them because the sample came out level would be fitting the code to 100 works in the
other direction. The regressions are the honest cost and are recorded as such.

**Cost $0.04.** Cumulative $0.99 of $20.

### Holdout — run once, on 100 works never looked at

| | wrong_settle | correct_settle | open | spend |
| - | -----------: | -------------: | ---: | ----: |
| dev, iteration 9 | 2 | 69 | 29 | — |
| **holdout** | **2** | **51** | 47 | $0.43 |

**Holdout wrong-settle is 2, dev's is 2. The gains generalise** — the rule asks for
within 5 points and the difference is 0. All 53 settled works were adjudicated and
every DOI checked against Crossref (`labels-holdout.json`, `payloads-holdout.md`); the
47 open works are not split further, because an open work is recoverable either way and
the holdout's job is the wrong-settle rate.

The two wrong settles are:

- **6982463097** → `10.1037/e413802005-319`, a PsycEXTRA conference record rather than
  Olson & Fazio (2001), the paper the surveillance task comes from. Right work, wrong
  kind of record — the same error as dev's 6906572393, so it is a known class rather
  than a new one.
- **2266446612** → `10.11575/prism/9529`, a repository record whose title is all but
  identical to the REPLICATION's own. Crossref holds no metadata for it, so nothing
  distinguishes it from a self-link under a second DOI. Counted wrong because it cannot
  be shown right.

**Yield is lower on holdout — 51 against 69 — and that is not overfitting on the
metric the rule tests.** The gap is in what the two samples' works allow: holdout
resolved 9 works from reference lists against dev's 20, so 11 fewer works had a
reference list to resolve from. The resolvers added in iterations 2–9 fire on the
same share of both.

### Stopping

Two consecutive changes have now failed both thresholds (iterations 5 and 9), against
a rule that asks for three. The maintainer was shown the bucket breakdown after
iteration 7 and directed: make the remaining mechanical fixes, consider a dynamic
shortlist, run one or two more validation rounds, then move to the holdout. Iterations
8 and 9 are those rounds. **The holdout runs next, once.**

Dev finishes at **69 correct settles and 2 wrong settles in 100**, from 24 and 25 at
baseline, for $0.99.

---

## Iteration 10 — one pooled candidate list, and what two reviews found

Raised by the maintainer after the first holdout: across the iterations, works kept
gaining links and losing them, because the two searches were EXCLUSIVE routes that each
dropped mechanically what its own guards doubted. Removing a false match is cheap — one
field of one LLM answer — and discarding a true one is expensive: the target goes
unresolved, the work returns to a worklist, and the whole ladder is paid for again.

So: every search that can say something about a target contributes to ONE pool, one
linking-model call decides over it, and the metadata rules survive as FLAGS beside the
candidate they doubt rather than as silent drops (`4c94932`).

Reviewed by codex and by Fable, both over the whole of the new code. What they found,
all now fixed:

| Finding | Fixed in |
| ------- | -------- |
| A pick that is not one of the candidates — a title, a DOI, an out-of-range number — was cached as a permanent decline | `ee3e0c9` |
| `author_year_candidates` keyed the raw topic truncated to 120 chars, while the query is built from `_topic_words` over the whole of it; the stopword list and the accent rule were not in the key at all | `ee3e0c9` |
| A title hit with no DOI but an OpenAlex id never entered the pool | `ee3e0c9` |
| **The self-link guard has never once run.** `title_search_candidates` refuses a hit whose title is the REPLICATION's at Jaccard 0.9, and every caller passed `""` as the replication's title. Holdout wrong-settle 2266446612 is exactly that class | `52721ab` |
| A record with no author at all was flagged rather than dropped. There is no judgment for the model to make and nothing to make it on | `52721ab` |
| The pick prompt still described the old author-and-year-only list, and never disclosed that it was showing 10 of 154 | `52721ab` |
| `candidates_total` described only the author-year half | `52721ab` |

**And the cost warning both reviews gave, which the first run proved.** Asking both
searches of every target roughly doubles the OpenAlex free-text bill on the titled
path, and a single 100-work sandbox run **exhausted the OpenAlex daily budget outright**
before it finished. The author-and-year query now runs only where the title search came
up empty or flagged: widening the net where the net came up empty is the point;
widening it over a clean hit buys a sibling-paper distractor and a second query at 10x
a filter query.

### What iterations 10–12 say

**Dev: yield 69 → 74, wrong-settle 2 → 1.** Five works gained a correct link and the
last-but-one wrong settle went. Every DOI Crossref-checked (`labels-dev-12.json`).

Two of the five had defeated four earlier iterations, and both were fixed by the pool
rather than by a rule:

- **"Bem (2011)"** → `10.1037/a0021524`, "Feeling the future". The title search alone
  matched Eysenck 1965, then an Oxford Handbook chapter; each guard moved the error
  rather than removing it. Pooled with the author-and-year shortlist, the right paper
  was in the list and the model took it.
- **"Svensson (AEJ: Macroeconomics, 2015)"** → `10.1257/mac.20130176`, the paper
  itself. Previously the journal's front matter, twice.

That is the maintainer's argument holding up: a candidate the model can see is a
candidate it can accept or reject; a candidate a rule removed is neither.

**Iteration 12 — the last wrong-settle class, named.** Both wrong originals that
survived to a holdout were an APA PsycEXTRA record: a conference abstract standing in
for the paper. APA files those under `10.1037/e<digits>-<digits>` and Crossref types
them `dataset`; the published article by the same authors carries an ordinary `10.1037`
DOI. `non_article_doi()` — the project's existing word for a DOI that is not a study —
now names them, and neither search offers one. The filter runs where the pool is built
rather than where the shortlist is fetched, because the shortlist is cached and a
fetch-time rule leaves every list already on disk carrying what the rule excludes.

### Second holdout — run once, on 100 works in neither earlier sample

| | wrong_settle | correct_settle | open | spend |
| - | -----------: | -------------: | ---: | ----: |
| dev, iteration 12 | 1 | 74 | 25 | — |
| **holdout 2** | **1** | **67** | 32 | $0.50 |

Drawn with seed 20260808 from the 1,125 works in neither the dev sample nor the first
holdout (`samples-holdout2.json`), because the first holdout has been read and a sample
that has been read is a second development sample.

**Holdout wrong-settle is 1, dev's is 1 — the gains generalise**, against a rule that
allows 5 points. All 68 settled works were adjudicated and every DOI checked against
Crossref (`labels-holdout2.json`, `payloads-holdout2.md`). 67 are right, including
Miguel/Satyanath/Sergenti (2004) for a Sub-Saharan climate-and-conflict replication,
Biber (1988) for a multi-dimensional register model, and Weinberg/Nichols/Stich (2001)
for a cross-cultural Gettier replication.

The one fault is 6887739870: "the relationship between personality and the processing
of task-irrelevant emotion" → Sander et al. 2005 on anger prosody and attention. The
paradigm is plausible and personality is not in it. Counted wrong because it cannot be
shown right.

**The PsycEXTRA class is gone from both samples**, which is what the second holdout was
for: iteration 12 was written against dev evidence, and an independent 100 works
carries none of it.

## Iterations 13–14 — CrossRef first, because it is free

Raised by the maintainer: OpenAlex is the metered path and CrossRef is not, so try
CrossRef and fall back. Three facts about CrossRef, measured on 2026-08-08, decide the
query and none of them was obvious:

- `query.author` is a RELEVANCE search, not an AND of the names. On a surname alone it
  is useless — "Jones Macken 1995" matches 6,182 works, "Han Kahn 2017" 41,372. The
  replication's own topic words have to go beside it.
- **Its results must not be re-sorted.** With `sort=is-referenced-by-count` the right
  paper for "Jones Macken 1995" is nowhere in the first three and the leader is a
  prefrontal-cortex PET study; in relevance order it is FIRST. Re-sorting a relevance
  search by citations throws away the only thing it knew. This is the opposite of the
  OpenAlex query, which FILTERS rather than ranks and does need a sort to choose among
  equals.
- It takes more topic words than OpenAlex, because it ranks where OpenAlex ANDs — but
  six returned nothing where five returned the paper, so the count relaxes.

**Iteration 13 let CrossRef replace the OpenAlex shortlist, and that lost nine links
while gaining eight** — the same replace-instead-of-add mistake iteration 9 made.
Iteration 14 gives it a sufficiency test instead: CrossRef ENDS the search only when
one of its hits carries **every** surname the citation named. "Jones and Macken (1995)"
comes back as a Jones-and-Macken paper and there is nothing left to buy; "Turri,
Buckwalter & Blouw (2015)" comes back as Turri-only papers, and the work OpenAlex finds
— "Knowledge and luck", all three authors — is the right one. A partial answer leads
the pool rather than replacing it.

**Result: 74 → 79 correct settles, wrong settles unchanged at 1.** Seven works gained a
correct link, every DOI Crossref-checked, including three that every earlier shortlist
had declined: Anderson et al. (2012) → "A status-enhancement account of overconfidence",
Zárate et al. (2004) → "Cultural threat and perceived realistic group conflict" (every
OpenAlex shortlist for "zarate 2004" was bipolar-disorder pharmacology), and Newman et
al. (2011) → "Celebrity Contagion and the Value of Objects".

**It also corrected a wrong original that had stood since iteration 4.** "Hamlin & Wynn
(2011)" now resolves to "Young infants prefer prosocial to antisocial others" — the
helper/hinderer habituation study the replication describes — where OpenAlex's
author-and-year shortlist had given the authors' OTHER 2011 paper. That is the class
that also cost a wrong settle in the first holdout.

One work moved the other way: "Yang et al. (2013), study of ambiguity aversion" now
links to Yang, Vosgerau & Loewenstein 2013 in the right journal on framing and
willingness-to-pay. Right authors, right venue, wrong subject; counted wrong.

### Iteration 15, and why it needed a third holdout

25 of the 101 open works across the three samples were title searches that found
nothing, and several of them had found the paper and thrown it away. Jaccard at 0.7
asks two titles to be nearly the same STRING, but the query is however the replication
quoted its target — usually a fragment. "Imagine being a nice guy: A note on
hypothetical vs. incentivized" scores 0.70 against the full title it is the opening of;
"the superiority effect in crowding" scores 0.67 against "The word superiority effect
overcomes crowding"; "Ambivalence of Neutral Ratings" scores 0.60 against "Do Neutral
Ratings Imply Indifference or Ambivalence?". All three are the paper. A hit is now
accepted when every content word of the query appears in its title, floored at three
words so "the martyrdom effect" cannot match the literature.

It is safe only because a model adjudicates the pool. While the first hit BECAME the
link, a generous retrieval was a generous supply of wrong originals.

**It moved nothing on dev** — its evidence came from holdout 1's misses, and holdout 1
had been read. So iterations 13–15 needed a holdout neither earlier one could give.

### Third holdout — run once, on 100 works used in none of the three samples

| | wrong_settle | correct_settle | open |
| - | -----------: | -------------: | ---: |
| dev, iteration 15 | 1 | 79 | 20 |
| **holdout 3** | **3** | **71** | 26 |

Seed 20260809 over the 1,025 unspent works; zero overlap with dev, holdout 1 or
holdout 2. All 74 settled works adjudicated, every DOI Crossref-checked
(`labels-holdout3.json`, `payloads-holdout3.md`).

**Holdout wrong-settle 3 against dev's 1 — within the rule's 5 points, so the gains
generalise.** But two of the three are classes dev never showed, and both are worth
fixing before the campaign:

1. **96773735 — a `resolved` self-link the guards cannot see.** The full-text rung
   "resolved" the paper to an OCR-mangled copy of its OWN title ("Report of an
   jnternship c:gndyctp1 lithe MemQrial Unjvmj'Y"), with no DOI, while its own evidence
   names Short's (1991) as the target. `doi_o == doi_r` cannot fire when both are empty,
   and the Jaccard-0.9 "never accept the replication's own title" guard lives in
   `title_search_candidates`, which the full-text rung does not go through. **This is a
   `resolved` row, so unlike a provisional one it would be imported for validation.**
   The fix is to apply that title guard where the row is built rather than where one
   search is.
2. **6925248538 — `no_original_found` on a work whose abstract names its original.**
   "the study by Sela et al. investigating the effect of assortment size on option
   choice". The target prompt named NO target, so iteration 1's rule — a named target
   that could not be identified writes `target_pending` — never fires. A work that
   names an original in its abstract and gets no target named is a failure of the
   prompt, not evidence that no original exists.

The third, 7028210935, is a Civile-2019 face-inversion paper standing in for another
Civile 2019: right author, right topic area, probably the wrong paper.

## Iteration 16 — where a no-identifier original actually comes from

The maintainer's question after holdout 3 — *"OpenAlex and CrossRef are unlikely to
ever return no identifier. Are we not saving the OpenAlex ids?"* — was the right one,
and the answer was no on both counts.

Such an original is not a CrossRef or OpenAlex record at all. It is a reference GROBID
parsed out of the PDF, whose title the OCR mangled: "Report of an jnternship c:gndyctp1
lithe MemQrial Unjvmj'Y", `authors_o` = "B.". And separately **we were discarding the
identifier we had**: `_merge_multi_row` used the record's `openalex_id` to build the
`pair_id` and then dropped it, so a DOI-less OpenAlex reference record reached the guard
carrying nothing and spent a title search re-finding what it had been handed.
`_fill_work_ids` only ever derives that column FROM `doi_o`, so nothing else filled it.

Four changes:

1. The record's own work id reaches the row.
2. A DOI-less record is also searched by **author and year**. `resolve_doi_by_metadata`
   scores candidates by title similarity, so on a mangled title it cannot succeed
   however well the paper is indexed; the author and the year survive OCR better.
3. What is still unidentified is **kept and flagged**, not written `resolved`: link
   method `unidentified_original`, its own set-aside file. This is deliberately not a
   similarity test. Measured over 277 correct links, title Jaccard between an original
   and its replication reaches **0.73** ("Rurality in England and Wales 1981" against
   "…1991: A Replication"), while the two real self-links scored **0.50 and 0.17** —
   one because OCR had mangled the title it copied. Similarity does not separate the
   classes at any threshold; identifiability does.
4. Before `no_original_found` closes a work whose own text cites somebody, that
   citation is looked up and the model is **asked** about it. The maintainer's
   correction to my first proposal: a veto would hold open every paper that says "we
   replicate X because Smith (2010) argued replications matter". If the model
   recognises the cited work the paper has a link; if it declines, the verdict stands
   and the row records the question rather than one reader's silence.

**A final codex review found six more, three of them campaign blockers**, all fixed in
`2591c20`. The one worth repeating: the title-search cache key named the response shape
but not the rule deciding which hit is returned — so every miss cached under Jaccard
0.7 would have replayed as a miss under the containment rule. **That is why iteration
15 measured as a no-op**; it was never actually exercised.

**Dev finishes at 80 correct settles and 1 wrong.**

## The pre-fix works, re-extracted live

`redo-pre-fix-29.txt` holds **27** ids, not the 29 the earlier note said — counted off
the file. **`--redo` turned out to be unnecessary**: none of them still carried a live
CURRENT-generation decision, because sixteen `EXTRACT_LADDER_VERSION` bumps had already
minted new generations and `_by_work` counts only current ones. A plain `--only` live
run reached all 27.

Result: 7 resolved, 13 provisional, 7 back to `target_pending`. $0.14. Nineteen of the
twenty new links were checked against Crossref and are right — Toya & Skidmore (2007),
Miguel/Satyanath/Sergenti (2004), Nunn & Wantchekon (2011), Bem (2011),
Moss-Racusin et al. (2012), Peri & Yasenov, Coutanche & Thompson-Schill.

**One is wrong, and it is a `resolved` row — the kind the validation import takes.**
Work **3124119366**, "Firm size and the use of export intermediaries", is linked to
`10.48548/pubdata-2261`, "Relevance and Detection Problems of Margin Squeeze – The Case
of German Gasoline Prices". Its own `link_evidence` names the right paper: "Jennifer
Abel-Koch, Who Uses Intermediaries in International Trade? … The World Economy (2013)",
which two other works in these samples resolved correctly. `doi_o_verification` says
`verified`, and is right to: the DOI does describe that gasoline paper. The verifier
checks DOI-against-title, never title-against-target, so it cannot see this.

This is a target-selection error at the full-text rung — the model marked a keyed record
`match_certain` and the record was the wrong one. **No mechanical check for it survived
testing.** The obvious one — "the evidence names an author and a year, so the linked
record should carry one of them" — was measured over all 304 settled links in the four
samples plus this run: it flags 4 and **all four are false positives** (preprint-vs-
published year gaps: Abel-Koch SSRN 2011 vs World Economy 2013, Békés 2015 vs 2016,
Peri & Yasenov 2018 vs 2019, and a dataset year "NHANES 2017") and it does **not** flag
the real one. It is not shipped.

What would address the class is **issue #186's Shape 1**, word for word: "here is the
target as the paper named it, and here is the record we found; is this plausibly the
same paper?" The issue scopes that to targets matching NO keyed record, and this one
matched one — so the scope is what needs widening, not the design. Commented there.

One difference is worth designing around rather than assuming away. On the search path
the model adjudicates records it has not seen; on the keyed-record path it would be
second-guessing an assertion it made itself, in the same call, moments earlier. That is
a weaker check on its face, and the search path's precision does not transfer to it.

**Built and measured 2026-08-08, later the same day.** The check is a SEPARATE call
(`confirm_keyed_original` in `shared/llm_client.py`, prompt
`build_keyed_confirm_prompt`), so the model is not grading its own answer in the same
breath: it sees only the study's title and abstract, the quoted evidence, and the
linked record's title/author/year/DOI — deliberately nothing a stored result row
cannot reconstruct, so the offline measurement
(`analysis/stage3_eval/keyed_confirm_eval.py`) exercises exactly the prompt the
ladder ships.

Measured over **all 63 LLM-accepted keyed link rows** in the latest result payloads
of the four evaluation batches plus the live re-extraction (18 + 11 + 9 + 18 + 7) —
per row, the same scope the shipped check runs at:

- The one known-wrong link — work 3124119366, the gasoline paper — is flagged,
  confidently, for the right reason ("unrelated in subject and authorship to
  Abel-Koch's firm-level trade/intermediaries paper").
- **Zero false positives** over the 62 correct links, including the
  preprint-vs-published year gaps (Abel-Koch, Békés, Peri & Yasenov) that made the
  mechanical author/year check flag 4-for-4 wrong.
- Cost: ~$0.02 of `gpt-5.4-mini`; no OpenAlex calls at all.

Wired into `_finalise_row` (ladder version 17, `_confirm_keyed_row` in
`extract/run_extract.py`), downstream of the `targetoutcome` cache and after
`_verify_row`, so it judges the record as corrected. Only a **confident** "not the
same paper" acts, and it demotes rather than drops: the row keeps its link, its
outcome and both readings, and moves to `link_method = keyed_link_disputed` —
provisional, settled, quarantined to `keyed_link_disputed.csv`, not imported — for a
human to arbitrate. An unconfident "no" flags and keeps the link; no answer writes
`api_error` so an unchecked link cannot settle on a transient failure. Sandbox
re-run of work 3124119366 confirmed the end-to-end demotion (batch
`keyed-confirm-sandbox`).

What is measured: the false-positive rate over 62 correct links, plus the one catch,
adjudicated against Crossref. Open: precision over a population of wrong links, and
the issue's "precision on human-confirmed rows" — both to be measured off the
`keyed_link_disputed.csv` rows as humans resolve them.

### Where the exercise finishes

| | baseline | final |
| - | -------: | ----: |
| dev correct settles | 24 | **80** |
| dev wrong settles | 25 | **1** |
| holdout wrong settles | — | **3** (holdout 1: 2, holdout 2: 1) |

Three independent holdouts, 300 works never used to design anything, adjudicated
work by work against Crossref: 2, 1 and 3 wrong settles.

Total spend, read off `cache/token_usage.json` against the snapshot taken before
iteration 0 rather than added up from the table: **$2.05** of the $20 approved —
2,373,444 input and 729,760 output tokens on `gpt-5.4-mini`, across roughly 1,200
work-runs.

`campaign.sh` beside this file is the runner the campaign uses. It gates
`extract.export` on the tier succeeding, because `export` renders the current
generation WHOLE and an export after a partial run replaced `data/extracted.csv`'s 285
rows with 6 (2026-08-07).

### What the campaign needs, beyond the LLM bill

**OpenAlex, not tokens, is the binding constraint.** The evaluation's LLM spend is
$2.05 over roughly 1,000 work-runs; the campaign's will be a few dollars. OpenAlex is
another matter: it bills a free-text `.search` query at 10x a filter query against a
DAILY budget that resets at midnight UTC, and the pooled resolver's queries are all of
that shape.

Measured, not estimated:

- **Before the `clean_hit` gate**, a single 100-work sandbox run exhausted all five
  keys' daily budget and died at roughly work 56 (`OpenAlexQuotaExhausted`, 2026-08-07).
- **After it**, a 100-work run of entirely fresh works — holdout 2, no cached
  shortlists — completed inside one day's budget.

So one fresh 100 fits in a day and 1,314 do not. The campaign is a **multi-day run, or
a funded one**: either about a fortnight at one batch a day, or prepaid credit at
<https://openalex.org/pricing>. This is a decision for the maintainer before the
campaign starts, not something to discover at work 56 of 1,314. The tier already stops
cleanly on exhaustion and every settled work stays settled, so a run split across days
resumes without losing anything.

### The goal this file was written for

`docs/stage3-quality-handover.md` set the exercise and said to delete it once its goal
was met. It is met and it is deleted; this file is what replaces it. The two things it
asked for that are NOT done, and are not part of its goal:

- ~~the 29 works carrying live settling verdicts from the pre-fix runs~~ — **done
  2026-08-08**, see below. There were 27, not 29: counted off the file rather than from
  the earlier note.
- issue #186 — measuring the provisional resolvers' precision on human-confirmed rows —
  is still open, and the 21 provisional links this exercise checked by hand are not a
  substitute for it.

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
