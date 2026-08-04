# Filter spec conventions

Policy for the issue #146 filter engine: what a pile means, where a precedence
belongs, and what evidence lets a rule discard on its own. `conventions.json`
next to this file is the machine-readable half, read by `filter/engine/export.py`;
this file is the reasoning. `docs/filter-engine.md` remains the authority for
module interfaces.

**Editing either file is a reviewed git change, not a code release.** A policy
change lands as a diff a reviewer can read without reading the engine. Neither
file is a filter, so neither is loaded as a spec — `conventions.json`,
`aliases.json` and `holdout.json` are all excluded from the spec glob.

`conventions.json` is nonetheless part of `bundle_hash()`. It decides what a pile
is *called* in an export, so a release that did not bind it could be exported
under a different status mapping than it was routed under; editing it mints a new
release, and an export against a stale bundle is refused. `aliases.json` reaches
the release id by its own hash (`alias_release`) and `holdout.json` names an
evaluation set that changes no row's status, so neither is in `bundle_hash()`.

## Pile → `filter_status`

| pile | `filter_status` | `filter_confidence` |
| --- | --- | --- |
| `discard` | `false_positive` | high |
| `screen_expensive` | `replication` / `reproduction` (by the winning rule's `vocabulary`) | high |
| `screen_cheap` | `needs_review` (or the rule's `vocabulary` at medium, if it names one) | medium |
| `needs_human` | `needs_review` | low |
| `pending` | not exported | — |

`vocabulary_names_status` in `conventions.json` is what "by the winning rule's
vocabulary" means: when true and the winning rule names a vocabulary, that
vocabulary becomes the status; otherwise the pile's `filter_status` stands.
`phrase-with-cite` — currently the only `screen_expensive` rule — names no
vocabulary, because a row carrying both vocabularies is exactly what the
two-voter screen exists to settle.

`filter_method` is `engine:<release_id_prefix>`. `filter_evidence` is `rule:<id>`
plus the evidence the backend recorded (phrase, prefix, type…).

## Precedence bands

Higher wins. Multi-match is expected and normal; the pile resolves once.

| band | contents |
| --- | --- |
| 900–999 | structural discards — the row is not a study at all (DOI prefix, work type) |
| 600–699 | rescues — evidence that outranks a discard, e.g. `exclusion-rescue` (#44) |
| 500–599 | vocabulary-exclusion discards — the seven exclusion patterns |
| 300–399 | `screen_expensive` routes |
| 200–299 | `screen_cheap` routes |

A new rule takes a free number inside its band rather than displacing a
neighbour: the numbers are only ever compared, never counted, so gaps are free
and renumbering an existing rule changes routing for rows nobody was thinking
about.

## `pending_reason`

`pending` is assigned by the engine, never by a spec — `validate_spec()` rejects
a spec that names it. The four reasons: `unevaluated` (no release has routed
this row), `no_filter_matched` (routed, nothing claimed it), `no_text` (a
screening pile was resolved but `abstract_text` is empty), `budget_blocked` (an
LLM tier was owed the row and could not pay for it).

`no_text` is engine policy, not a spec: absence of evidence must not convert
into a proceed. Structural discards are unaffected — they never read the
abstract.

## Measurement levels

A `measured` entry says how the rule's precision is known. Four levels permit a
rule to discard autonomously:

- `human` — someone read a sample of what the rule discards.
- `llm:<model>` — an LLM read a sample; the model id is part of the claim.
- `downstream` — a later stage's verdicts on the rule's rows were counted.
- `trusted` — the mapping is structural rather than statistical (a DOI path
  segment minted for a review object; a registry work type). Needs a rationale
  saying *why* no sample is needed, not merely that none was taken.

`heuristic` is shadow-only. It records a guess, and a guess may not delete
records: `validate_spec()` rejects a non-shadow `discard` whose evidence is all
heuristic, exactly as it rejects one with no evidence at all.

An unmeasured discard runs as `"shadow": true` — evaluations are recorded, the
pile is unaffected — until the diagnostics that would measure it exist.

## `pyre_regex`

A loader-only extension key inside a `match` object, legal only on a match that
is decomposed into `any_of`/`all_of`/`none_of`. It holds the original Python
`re` pattern for a rule whose faithful form needs a lookaround; the decomposition
next to it is what RE2 (and so pyarrow) evaluates. Two rules use it —
`biological-of` and `data-availability` — and both are `shadow` because their
decompositions widen the discard. `filter/phrase_detection.py` reads the
`pyre_regex` form, which is what keeps `keyword_verdict()` unchanged.

## `aliases.json`

`{"version": 1, "aliases": {}}` — a flat map from a superseded OpenAlex work id
to its canonical one, both as bare integers written as JSON object keys/values
(`{"2741809807": 4210170740}`). Empty to start. JSON has no comments, so any
provenance for an entry belongs in the commit message that adds it. Aliases
resolve before any state is keyed by `work_id`, and the file's hash is one of
the inputs to the routing release id.

## Known intended divergences from `keyword_verdict()`

Four, recorded and reasoned in `docs/filter-engine.md` §"Known intended
divergences": the concept arm routing instead of killing; one RE2-safe cite
regex replacing the same-sentence gate and the bare-name blacklist; row-scoped
rather than sentence-scoped GWAS guards; and no-abstract rows going to
`pending/no_text` instead of being screened blind. Parity is measured against
those intended semantics (#148), so a parity report that flags one of them is
reporting a known divergence, not a bug.
