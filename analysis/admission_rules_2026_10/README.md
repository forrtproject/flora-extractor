# Pipeline admission expansion — 6 October 2026

Add three live `screen_expensive` admission rules, leaving the broad text,
residual and reproduction families in shadow. The intended operating target is
roughly **more than 50% eligible candidates**, with preference for useful-sized
additional pools. These rules buy normal two-voter screening; none automatically
accepts a paper, assigns a vocabulary, or discards it.

The following table describes the **original measured patterns**, before the citation
syntax correction. It is historical evidence, not a precision estimate for the revised
rule. The revised reach and candidate losses are reported below.

| Original rule | Additional available works | Fresh usable / read | Estimated usable yield |
| --- | ---: | ---: | ---: |
| replication-claim-extend-prior | 2,267 | 43/60 | 71.4% |
| replication-claim-cited-abstract | 1,585 | 35/60 | 59.8% |
| replication-claim-failure-prior | 123 | 26/27 | 78.0% |

The original marginal union was **3,975 available OpenAlex works** beyond the pre-change
live bundle. Individual total reaches are 2,267 / 1,628 / 129; overlaps are removed
in the extension → citation → failure order used for both attribution and sampling.
The original roughly 71% / 60% / 78% estimates implied about 2,663 model-clear candidates;
these are planning estimates, not final validated records. Precision here means
model-judged clear completed replication/reproduction under project scope, with
matched supporting quotes and no obvious metadata mismatch. It is not a measured
production screen proceed rate, and it is not independent human validation.

## Matching and precedence

- `replication-claim-extend-prior` (738): replicate-and-extend wording in title or
  abstract, plus an earlier-study/finding/result reference anywhere in that text.
- `replication-claim-cited-abstract` (737): first-person or present-study replication
  verb in the abstract, followed within 120 characters and the same clause by
  a capitalised name followed by a year, or a year followed by named authors.
  Year-first references require either “by” before the name or a study noun
  after the author names. This is bibliographic syntax, not proof that a capitalised
  word is an author, that the author names the target, or that the study completed.
- `replication-claim-failure-prior` (736): first-person failed/unable/attempted to
  replicate in the abstract, plus the earlier-study/finding/result anchor.

All require an abstract and inherit the live general rule's repository DOI and
replication-package title exclusions. Existing title rules (740–760) and curated
admission (745) retain precedence; the new rules sit above general (735), below
structural discards (935–960), and above the fallible type crosswalk (500).
No precedence was renumbered. Claiming some previously admitted rows changes
attribution, not destination. No screen/extraction prompt, model or generation
changed; only the next routing release's bundle changes.

The completed-action/named-target micro-rule previously proposed yields only
56 additional works and is unnecessary at this operating target: its useful
matches are captured by these larger measured admissions. Broad bare
replicate-and-extend (45% clear), independent replication (7.5%), and reproduction
families (roughly 28–45%) are not promoted on the existing evidence.

## Evidence and sampling

`manifest.json` names the frozen pre-change commit (`f4225e6`), live bundle, pool,
overlay and FLoRA hashes. The offline measurement read 5,146,160 survivor-pool rows
through the engine's overlay reader and actual RE2 evaluator. No production store
or remote state was written. A work already admitted on any sibling row was
excluded: this is conservative relative to first-writer routing in the store.
Higher-priority discards stay excluded; the type crosswalk is overridden normally.

Available pools exclude FLoRA `doi_r`, the exported extraction file, and the frozen
validated skip list. Known FLoRA matches are not a recall denominator here: these
are measurements of incremental candidate yield. Other quarantine files were
not used as a novelty authority. Versions can remain separate OpenAlex works.

574 distinct marginal works were read: 427 already judged in the earlier blind
research pilots and 147 fresh works. After removing preceding new rules, sample
60 works uniformly from each remaining unreviewed large pool (seed 20261007).
The failure pool had only 27 unreviewed members, so read all 27. The 147 new works
were shown to headless `gpt-6.1-sol` at medium effort without rule names. Its labels
were clear / uncertain / no / prospective. Unknown completion/intent/target cases
stay uncertain; prospective plans are not counted clear. A separate blind
metadata audit flagged no obvious title/abstract mismatches in the new batch.
One otherwise-clear extension quote did not match its source, reducing 44 raw
clear labels to 43 usable. Earlier members retain the same quote/metadata checks.

The precision estimator treats previously reviewed members as a fully observed
stratum and weights the fresh random sample over the remaining population:

```
(previously usable + remaining population × fresh usable fraction) / marginal population
```

See `summary.csv` for the conditional Wilson interval after weighting. The cited
abstract rule has a point estimate above 50%, but its interval includes 50%; this
is an acceptable candidate-screening risk under the requested operating target.
The failure rule is a census of all 123 marginal works, with 96 usable. A census
removes sampling uncertainty for this frozen pool, not model-label error.

`screened_cases.csv` preserves every judgment, title, abstract, evidence quote,
quote-check result, metadata flag and sampling stratum. `populations.csv` contains
the denominators and `summary.csv` the derived estimates. Recount without API or
production writes:

```bash
.venv/bin/python -m analysis.admission_rules_2026_10.summarise
```

Paid API pilot spend was **$0**. Headless Codex used the existing ChatGPT login.
The labels are research evidence, not permanent production screen verdicts.
Adding the specs runs no routing or screening campaign and spends no pipeline
budget. The normal next route picks them up after the change lands.

## Citation correction after measurement (2026-10-12)

The original regex was case-insensitive throughout: `[A-Z]` matched any word, so
“with 2000 participants” and “using 2010 protocols” looked like citations. A review
revision kept that original arm OR a more explicit citation arm. That preserved the
noise as well as real claims. The final rule removes the original arm.

Two positive bibliographic syntax families share the same first-person/present-study
claim prefix and bounded clause context:

- **Author before year:** a capitalised ASCII/Latin-1 name at a whitespace or opening
  parenthesis/bracket boundary, optional “et al.”, “and colleagues” or a study noun,
  optional comma/parenthesis, then a 19xx/20xx year. Possessives are allowed within
  the name spelling. The name is case-sensitive; years have a right boundary.
  A whitespace/comma/open-parenthesis separator is required before the year.
- **Year before author:** a year followed by “by” and a capitalised name, or by
  capitalised names joined with “and”, “&” or “et al.” and an explicit study/paper/
  work/findings/results/analysis/experiment noun. The “by” name must end at a
  delimiter or end of text. Citation whitespace is horizontal: a citation cannot
  continue onto another line.

These are syntax families, not exceptions for specific surnames, papers or work ids.
Capitalised ordinary/proper nouns can still resemble author names; the screen decides
what the paper means. The name alphabet does not cover every writing system. Bare
survey dates, sample sizes and other word-plus-number strings are deliberately outside
this citation rule. Some useful replication claims use those other forms, and this
rule does not promise to recover every one. Existing admissions may independently
reach them; otherwise they follow the baseline routing, often pending. The revised
rule adds no discard predicate.

The two positive families are one top-level regex so evidence recovery reports the
matched phrase before the abstract-presence guard. Tests exercise RE2 through the
actual engine and Python's span recovery, including accented names, possessives,
year-first syntax, embedded capitalised substrings, larger numbers and clause boundaries.
An independent subagent reviewed the syntax for overfitting and found no blocking issue.

`citation_review_patterns.json` freezes the three earlier comparators.
`audit_citation.py` compares those and the revised live spec over the pool with its
text overlay, without API calls, routing writes or a screening campaign:

```bash
.venv/bin/python -m analysis.admission_rules_2026_10.audit_citation
```

The resulting `citation_review_summary.json` records the pool/overlay/bundle hashes,
reach and routing counts; `citation_review_cases.csv` preserves old-only matches and
changes from the previous OR, with source text and any existing model labels. Flags
ending in `_work` describe membership on any sibling row; the unsuffixed flags
describe the displayed row itself. A representative row need not illustrate every
whole-work match. The
old-only set is defined before higher-priority discard and existing-record exclusions;
the routing count separately respects precedence. Both exclude baseline-admitted works
on any sibling row. Neither applies FLoRA/export/validation skip lists, so neither is
the original available-work denominator.

Final recount: **5,146,160 rows**, with the same pool and overlay hashes as
the historical measurement; **28,095** baseline-admitted works.

| Citation pattern | Reach beyond the baseline, before higher-priority discards and existing-record skips |
| --- | ---: |
| Original measured word-plus-year | 1,884 |
| Previous broad OR | 2,404 |
| Previous citation arm alone | 2,021 |
| Final explicit citation syntax | 2,027 |

This scope includes overlap with the new extension/failure rules. It is not the
earlier 1,809/2,301 comparison scope or the original 1,585 available-work denominator.
There are **383 old-only works** in this explicitly defined scope; the final
year-first grammar retains **12**. Relative to the previous OR,
**387 works are removed and 10 added**.

After actual routing precedence and extension → citation → failure attribution,
the three new rules add **4,217 routable works** before existing-record skips:
2,325 extension, 1,767 citation after extension, and 125 failure
after both. These are scheduling counts, not screening proceeds or extracted records.

Of the 311 previously labelled citation cases, 291 still match;
193 of the 204 previously model-clear/actionable cases are retained, and
11 are lost. The losses include nameless temporal-survey claims and
noncanonical author references that the narrower syntax does not parse. They are
preserved in the changed-cases artifact; removing noise does not mean every removed
record is out of scope. Extension and failure retain all their previously labelled
cases. These selected-subset counts are not a fresh precision estimate.

Validation: **2,020 passed, six optional live tests skipped**, with a dummy OSF token
and blocked network; **139** focused engine/spec/snapshot tests passed. An independent
subagent approved the final regex after 27 repository edge cases plus 19 independent
formatting/boundary examples agreed across RE2 and Python. It also checked the audit's
admission projection against actual routing on 52 synthetic rows, including textless
ordinary/OSF records and stronger/weaker discards.

The old 59.8% estimate applies only to the original **1,585-work marginal available
population**. The revised rule has `measured: []`: no fresh precision estimate has been
established, and retained labels are a selected subset rather than a new random sample.
The extension rule's hyphen support remains; the failure rule and precedence are unchanged.
