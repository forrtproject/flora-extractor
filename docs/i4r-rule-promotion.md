# Live reproduction-claim admission

`filter/spec/reproduction-claim.json` enables the reviewed reproduction-claim rule:
`shadow: false`, `pile: screen_expensive`, `admission: strict`, precedence **734**.
It assigns no `vocabulary`: the screen decides replication/reproduction eligibility.

Four arms recognise explicit reproduction-report titles, computational/robustness
reproduction of a target in the title, bounded active self-reproduction claims in
a comment/reply/reanalysis/replication genre, and explicit self-claims to assess
or conduct computational reproduction, including past-tense and third-person
forms. Arm 4 permits up to five determiners, qualifiers or assessment words
between the action verb and computational reproduction/reproducibility. It does
not treat testing a framework or tool as testing computational reproducibility.
Each matching clause stands on its own: a tooling sentence cannot cancel a genuine
claim elsewhere in the same abstract. The positive pattern uses RE2-compatible
syntax; the engine does not support negative lookahead. Its closed grammar is
conservative and can miss indirect claims. Generic `present`/`provide` announcements,
passive copyright boilerplate and “species which reproduce” grammar do not activate
this arm. Broad reproduction or reanalysis wording alone is insufficient.

The rule yields to structural discards and narrower replication-claim rules. It
outranks the fallible study-type crosswalk, consistently with the other claim
rules. Strict admission requires qualifying, confident screen votes; the ordinary
text requirement and alias deduplication still apply. Replies are eligible only
when a claim arm matches; they are neither admitted nor excluded as a class.

## Measured scope

The 2026-11-10 replay read the operational **5,146,160-row survivor pool**, used its
text overlay and alias map, selected the first physical row per canonical work in
pool filename/row order, and evaluated both the current book and promoted rule
through the engine's own PyArrow backend. It made no LLM calls or operational
state changes.

| Replay quantity | Works / DPs |
|-----------------|-------------|
| Canonical first rows replayed (original/reviewed candidate union) | 350 works |
| Reviewed-rule matches on canonical first rows | 336 works |
| Formerly pending works newly routed to expensive screening | **221 works** |
| Existing discards newly routed to screening | **0 works** |
| Candidate matches still deferred for missing text | 20 works |
| Pending I4R DPs newly screenable | **69 of 189 DPs** |

The frozen inputs are committed as
[`misc/i4r-rule-candidates-2026-11-10.jsonl.gz`](../misc/i4r-rule-candidates-2026-11-10.jsonl.gz).
They contain the 350 canonical candidate rows, including the effective text overlay
and DP-to-work associations. Their SHA256 is
`e10e9d9ace9003c564f3955d3a08c3932ef0a7dbb0ec675a031f95c6347d053a`.
Replay them without the operational pool, store or API access:

```bash
.venv/bin/python -m analysis.i4r_rule_replay
```

The command routes the frozen rows with and without the current reproduction rule;
[`misc/i4r-rule-replay-2026-11-10.json`](../misc/i4r-rule-replay-2026-11-10.json)
records its output. The input cohort is the union selected by earlier broad claim
patterns, rather than a new scan of the full pool. The final clause grammar narrows
the broad positive arm used to select that union. The current book and aliases are
read from `filter/spec`; later changes there can change the replay results.

The final clause-level rule admits 221 pending works, seven fewer than the earlier
228-work review version, while retaining the same 69 I4R DPs. It does not let an
unrelated tooling sentence veto a genuine claim. The other candidate matches were
already screened or remain discarded. The 69 is a gain in admission, not a count
of confirmed reproductions. Independent precision has **not** been measured;
strict admission is retained. The curated I4R corpus suggested the patterns, so its
recovery is not an independent precision test.

Activation of the JSON rule occurs when this branch is merged. A new routing
release is then required; `PENDING_RUNS.md` records the command and evidence to
check. No paid screen/extract campaign or production CSV export is part of the
promotion commit.

## The Stage 1 gap is separate

The shared pool manifest and sampled first/middle/last shards were uploaded on
**2026-08-03**; the latest partition name is **2026-06-26**. The remote manifest
has an empty `snapshot_date`, so its upload date must not be relabelled a snapshot
cutoff. The local pull on 2026-10-28 did not refresh the underlying snapshot.

Of the 70 live OpenAlex matches not found in that pool:

| First OpenAlex indexing | Pass the identical gate now | Fail now |
|-------------------------|-----------------------------|----------|
| By 3 August | 2 | 30 |
| After 3 August | 29 | 9 |
| Total | **31** | **39** |

The two earlier passes were first indexed on 24 and 26 July, after the pool's
latest partition date. All 31 passes therefore look like freshness misses, while
39 would still fail the current gate. This is a replay on current metadata, not
an audit of historical rejected raw records.

The 189 pending DPs already passed Stage 1: 185 match no current live admission
rule and four are deferred for missing text. The broad dormant
`reproduction-signal` matches 127 of them, but includes unmeasured reanalysis and
other senses; it remains in shadow.

[Complete linked list of the 39 gate failures](i4r-search-gate-misses.md) and
[CSV](../misc/i4r-search-gate-misses-2026-11-10.csv). They include critiques,
responses and metaresearch as well as potential reproductions; membership in the
series is not a replication verdict.
