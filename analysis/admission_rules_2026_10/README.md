# Pipeline admission expansion — 6 October 2026

Add three live `screen_expensive` admission rules, leaving the broad text,
residual and reproduction families in shadow. The intended operating target is
roughly **more than 50% eligible candidates**, with preference for useful-sized
additional pools. These rules buy normal two-voter screening; none automatically
accepts a paper, assigns a vocabulary, or discards it.

| Rule | Additional works | Fresh usable / read | Estimated usable yield |
| --- | ---: | ---: | ---: |
| replication-claim-extend-prior | 2,267 | 43/60 | 71.4% |
| replication-claim-cited-abstract | 1,585 | 35/60 | 59.8% |
| replication-claim-failure-prior | 123 | 26/27 | 78.0% |

The marginal union is **3,975 available OpenAlex works** beyond the pre-change
live bundle. Individual total reaches are 2,267 / 1,628 / 129; overlaps are removed
in the extension → citation → failure order used for both attribution and sampling.
The roughly 71% / 60% / 78% estimates imply about 2,663 model-clear candidates;
these are planning estimates, not final validated records. Precision here means
model-judged clear completed replication/reproduction under project scope, with
matched supporting quotes and no obvious metadata mismatch. It is not a measured
production screen proceed rate, and it is not independent human validation.

## Matching and precedence

- `replication-claim-extend-prior` (738): replicate-and-extend wording in title or
  abstract, plus an earlier-study/finding/result reference anywhere in that text.
- `replication-claim-cited-abstract` (737): first-person or present-study replication
  verb in the abstract, followed within 120 characters and the same clause by
  author-year-style text. The engine is case insensitive: this is proximity to a
  reference-shaped string, not proof that a capitalized surname names the target.
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
