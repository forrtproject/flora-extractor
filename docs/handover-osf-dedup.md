# Handover: deduplicate OpenAlex works that name one OSF record (issue #200)

**Nothing is implemented.** The prerequisite is committed and the decision is made;
the derivation script, the alias entries and the re-route are all still to do. This
brief is what you need to start without re-deriving any of it.

## The defect

OpenAlex mints more than one work for the same OSF object, and nothing in the pipeline
derives identity from the OSF guid. Each work is therefore screened, extracted and
shipped separately, so one study reaches the validation import as several rows.

Measured on `data/extracted.csv` (2,602 rows) and release `56076eb48fda`:

| | Count |
| --- | ---: |
| OSF records reached by >1 OpenAlex work id | 117 |
| Surplus records | 173 |
| **Rows involved** | **296 of 2,602 (11%)** |
| Existing aliases covering any of them | **0** |

They are the same record, not near-duplicates — same title, same coded outcome:

```
qp4h8  W2776696688  'Relationship of 2D:4D Ratio to the Big Five…'  cannot_be_determined
qp4h8  W7070882364  'Relationship of 2D:4D Ratio to the Big Five…'  cannot_be_determined
qp4h8  W7110500188  'Relationship of 2D:4D Ratio to the Big Five…'  cannot_be_determined
```

## The rule, as decided by the maintainer

**Same OSF guid ⇒ same record.** The canonical work is, in order:

1. a published article (a DOI on any registrant other than OSF's own), then
2. the OSF registrant DOI (`10.17605`), then
3. the record identified only by an `osf.io` URL.

Tie-break within a tier by the lowest work id — the oldest OpenAlex record, which is
the one most likely to carry citations and complete metadata.

A published article and its OSF copy **are** the same study and must not be coded
twice. That is a maintainer ruling, taken with the paired examples in front of it:

```
W6962839798  10.17605/osf.io/bjmyx    'Replication of Wilkins & Kaiser (2014)'
W4230164836  10.31234/osf.io/bjmyx    'Replication of Wilkins & Kaiser (2014)'
W4409657269  10.31234/osf.io/bjmyx_v1
```

**This is deliberately WIDER than `osf_identifier()`**, and the difference is not an
oversight. `osf_identifier()` refuses DOI-bearing rows so a backfill cannot overwrite
an article's abstract with a registration template line — a question about TEXT.
Identity is a different question. Keep that guard exactly where it is; do not reuse it
as the dedup predicate.

## Where it goes

| Piece | Path | Precedent |
| --- | --- | --- |
| Derivation script | `analysis/build_osf_aliases.py` | `analysis/build_validated_skip.py` |
| Output | `filter/spec/aliases.json` | 3,205 entries already there |
| Tests, if the canonical rule needs pinning | `tests/test_engine_workids.py` | `workids.resolve()` consumes aliases |

`aliases.json` is `{"version": 1, "aliases": {"<superseded>": <canonical>}}` with bare
integer work ids as JSON keys and values. JSON has no comments, so the provenance of a
batch of entries goes in the commit message — say which scan produced them, over which
pool, and under which rule.

## Prerequisite, already committed

`681556a` fixes `osf_registration_guid()`, which until then returned the path segment
in FRONT of a guid: `osf.io/download/hgwkv/` gave `download`. **Use that function; do
not write a regex.** My first scan carried a copy of the old pattern and produced a
"download" group holding 55 unrelated works — the totals in the issue body (10,818
groups, 15,478 surplus) come from that contaminated run and must not be trusted.

A clean re-scan is in `scratchpad/scan_osf_dupes.py` (rewritten to call the fixed
function). Re-run it rather than reusing its numbers if any time has passed.

Note the fixed extractor also strips a version suffix, so `d3x9p_v1` … `_v4` collapse
to one guid. That is intended: the versions of one preprint are one record.

## Validation before you write 15k aliases

A wrong merge silently fuses two distinct studies, and nothing downstream will say so.
Two checks are cheap and were already useful:

1. **Title agreement within each group.** On the contaminated scan, 90% of groups had
   an identical normalised title; the 10% that differed were truncation, SCORE-report
   metadata drift, and preprint versions with edited titles — all still one record.
   Re-run this on the clean groups and read a sample of the disagreements.
2. **Group size.** A group with dozens of members is the shape a mis-parsed guid makes.
   That is how #201 was found. Any large group deserves reading before it is trusted.

## Sequencing, and one trap

Adding aliases mints a new `alias_release`, so it needs a re-route to take effect.
Two other committed changes are waiting on a re-route as well: the frozen OSF overlay
chunk and the `no_text` exemption (`e0feb7d`).

**Carry all three in ONE route.** Each re-route mints a release, and a screen run
between two of them pays two voter calls per work for records about to be merged. The
open entries in `PENDING_RUNS.md` are ordered on that assumption.

After the route: re-export and confirm the row count falls by the surplus. That is the
acceptance test — not the alias count.

## What is NOT in scope

The dedup covers works sharing an OSF guid. Duplicate OpenAlex works that share no OSF
identifier are the same class of problem and are not measured here; do not widen the
script to guess at them without evidence.
