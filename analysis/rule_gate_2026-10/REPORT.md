# Rule-dependent admission gate — 28 October 2026

Four live screening rules keep the lenient gate. The other five, together with every
shadow screening rule, are strict. On release `61f07fd07f19`, this assignment
**defers 3,339 of the 24,170 works the screen proceeds (14%)**. 857 of these currently
ship in `extracted.csv`. 29 more would be deferred but are exempt, because a person
has already judged them in validation.

The gate does its job on the evidence available. Among validated strict misses,
validators marked 41% as not replications when only strict rules matched them
(12 of 29, the works now exempt). When a lenient rule also matched, the rate was 4%
(1 of 24).

## What the strict gate reads

`screen_strict()` in `shared/llm_client.py` needs two or more votes, and every vote
must be replication, reproduction or both, and confident. The pre-PDF title-search
rung already asks the same of its votes. The test reads the stored votes only, so it
works for the current DeepSeek + Luna pair, for the `gpt-5.4-mini`-era verdicts that
pair still accepts, and for PR #219's Luna + Span-02 pair. That pair's decision voter
writes `confident` at P(qualifies) ≥ 0.9.

## Rule assignments

The threshold is a measured base rate of about 70%. Below it, or with no measurement,
a rule is strict. The base-rate sources are the specs' `measured` entries and
`analysis/admission_rules_2026_10/`.

| rule | gate | base-rate evidence | proceeds | strict among proceeds | deferred |
| --- | --- | --- | --: | --: | --: |
| `replication-claim-cited-title` | lenient | 97.2% (Sonnet, n = 108) | 1,219 | 86% | 0 |
| `replication-claim-title-strong` | lenient | 94.1% (Sonnet, n = 237) | 2,115 | 79% | 0 |
| `replication-claim-failure-prior` | lenient | 78.0% (census, gpt-6.1-sol) | 299 | 93% | 0 |
| `replication-claim-extend-prior` | lenient | 71.4%, CI 59–81% (gpt-6.1-sol, n = 140) | 2,518 | 89% | 0 |
| `replication-claim-cited-abstract` | strict | 59.8% (gpt-6.1-sol, n = 311) | 2,087 | 93% | 150 |
| `replication-claim-general` | strict | ~50% clear (human, 9 of 18 proceeds) | 11,190 | 80% | 2,196 |
| `replication-claim-title-broad` | strict | unmeasured as a rule (arms 5–90%) | 2,594 | 72% | 711 |
| `curated-observatory` | strict | unmeasured | 1,646 | 89% | 167 |
| `osf-registration-completed` | strict | heuristic only | 502 | 70% | 115 |

Rows are attributed to the rule that won routing. A work's gate is the most lenient
of all the live screening rules it matched. Proceeds and deferrals come from the
stored live verdicts in the current screening generation.

## Supporting evidence per rule

| rule | Stage 3 endings of extracted proceeds: shipped / not_a_replication / no_original_found / prospective | validated proceeds: not a replication when strict passes vs fails |
| --- | --- | --- |
| `curated-observatory` | 69% / 0% / 1% / 0% | 10% (n = 29) vs 0% (n = 3) |
| `osf-registration-completed` | 21% / 4% / 5% / 31% | 22% (n = 9) vs 50% (n = 2) |
| `replication-claim-cited-abstract` | 57% / 0% / 1% / 14% | 12% (n = 43) vs 0% (n = 1) |
| `replication-claim-cited-title` | 58% / 3% / 0% / 9% | 1% (n = 94) vs 8% (n = 13) |
| `replication-claim-extend-prior` | 45% / 0% / 2% / 5% | 12% (n = 17) vs – |
| `replication-claim-failure-prior` | 53% / 0% / 0% / 2% | 0% (n = 3) vs – |
| `replication-claim-general` | 42% / 0% / 3% / 9% | 10% (n = 151) vs 60% (n = 10) |
| `replication-claim-title-broad` | 31% / 4% / 9% / 1% | 6% (n = 71) vs 38% (n = 13) |
| `replication-claim-title-strong` | 42% / 2% / 4% / 2% | 7% (n = 87) vs 0% (n = 11) |

Stage 3's endings cannot stand in for a base rate. Its own `not_a_replication`
verdict stays at 0–4% for every rule. That is the problem the gate addresses, not a
measure of it. The validator column counts a work as not a replication when at least
one validator corrected its type to `not_validation`. Across all rules, the rate is
8% for proceeds that pass the strict test (38 of 504) and 25% for those that fail
it (13 of 53).

## Precedence and the release id

- **The most lenient matching rule wins.** A rule's base rate is measured over every
  work it matches, overlaps included. Precedence only names the routing winner.
  Using the winner's gate instead would defer 3,384 works rather than 3,339.
- **The gate is outside the release id.** `bundle_hash()` drops `admission`
  (`UNROUTED_KEYS`). The gate moves no row between piles, and the verdicts it reads
  already apply across releases. Including it would turn every gate change into a
  re-route and a new release id. This machine cannot re-route at present, because
  the pool parquet is no longer on disk.

## What the deferred works already are

| ending | works |
| --- | --: |
| `extracted.csv` (ships today) | 857 |
| `target_pending` | 1,524 |
| `no_original_found` | 377 |
| `prospective_registration` | 154 |
| `search_link_unconfirmed` | 139 |
| `no_evidence` | 110 |
| `unidentified_original` | 70 |
| `not_a_replication` | 42 |
| other | 3 |
| never extracted | 63 |

Before the exemption, 992 deferred works had a record in the validation `unvalidated`
table: 963 unvalidated and 29 with a human type judgment (12 judged not a replication,
17 a replication). Those 29 are now exempt (`data/human_judged.csv`, 791 works, built
by `analysis/build_human_judged.py`). The next live `extract.export --release` would
retire the 857 shipped deferred works as `screen_deferred`.

## Limits

- The routing comes from the newest stored release, `61f07fd07f19` (2026-10-12).
  The rule book has changed since: the citation-syntax fix and the `peer-review-record`
  discard (#223). The pool parquet is no longer on disk, so the current bundle could
  not be re-routed. The counts are therefore approximate for today's bundle.
- The stored votes come from two pairs: 23,206 works from DeepSeek + `gpt-6-luna` and
  9,087 from DeepSeek + `gpt-5.4-mini`.
- The validated works are not a random sample, and most per-rule cells are small.
- The base rates in the assignment table are model-judged except
  `replication-claim-general`'s 18 human reads.

## Reproduce

The inputs this report was computed from are in `out/`. `pull.py` OVERWRITES them: it
reads the routing store, Supabase and the exported CSVs (writing nothing to any of
them), and a re-pull against today's store and tables gives today's inputs, not
these. `measure.py` re-derives the tables from `out/` plus the gate assignments in
the committed spec bundle and `data/human_judged.csv`:

```bash
.venv/bin/python -m analysis.rule_gate_2026-10.pull --store cache/engine/engine.duckdb --data data --release 61f07fd07f19
.venv/bin/python -m analysis.rule_gate_2026-10.measure
```
