# Filter spec conventions

Policy for the issue #146 filter engine: what a pile means, how precedence
resolves, and what evidence lets a rule discard on its own. `conventions.json`
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
| `screen_expensive` | `needs_review`, or the winning rule's `vocabulary` if it names one | high |
| `screen_cheap` | `needs_review`, or the winning rule's `vocabulary` if it names one | medium |
| `needs_human` | `needs_review` | low |
| `pending` | not exported | — |

`vocabulary_names_status` in `conventions.json` is the mechanism: it is true for
both screening piles, and when the winning rule names a vocabulary that
vocabulary becomes the status; otherwise the pile's own `filter_status` stands.
Whether a rule names one is the rule's own decision, recorded in its spec: a
`vocabulary` is a claim about what the row *is*, and a rule whose admission is a
request for attention rather than a verdict names none.

`filter_method` is `engine:<release_id_prefix>`. `filter_evidence` is `rule:<id>`
plus the evidence the backend recorded (phrase, prefix, type…).

## Precedence

Higher wins. Multi-match is expected and normal; the pile resolves once, by the
highest-precedence non-shadow rule. A shadow rule's precedence decides nothing —
it records where the rule would sit if promoted.

The bundle is a whitelist: nothing is screened unless a positive rule admits it.
Rules route and discard; only LLMs admit a row as a probable record.

A precedence is a claim about which other rules this one must outrank or yield
to, and the argument for that claim belongs in the spec's own `description` —
naming the rules it is ordered against and why. A new rule takes a free number
rather than displacing a neighbour: the numbers are only ever compared, never
counted, so gaps are free and renumbering an existing rule changes routing for
rows nobody was thinking about.

## `pending_reason`

`pending` is assigned by the engine, never by a spec — `validate_spec()` rejects
a spec that names it. The engine emits exactly two reasons: `no_filter_matched`
(the row was routed and no rule claimed it) and `no_text` (a screening pile was
resolved but `abstract_text` is empty, so the pile is downgraded).

There is no reason code for a row a release never routed: such a row simply has
no entry in the routing table, and absence there is the record. Nor is there one
for a budget-blocked LLM tier — a tier that cannot pay leaves the row sitting in
its pile with no verdict, which is a missing verdict rather than a `pending`
row.

`no_text` is engine policy, not a spec: absence of evidence must not convert
into a proceed. The discards are unaffected — none of them reads the abstract.

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

**A `measured` entry must say what it was counted over.** Two things count as
evidence, and an entry that carries both must keep them apart:

- an **exact count over a named population** — "3.9 extra rows per million of the
  5.6M-row snapshot scan" — where the population is identified precisely enough
  that someone could recount it; and
- a **reading of a sample of the rows the rule itself decides**, drawn at random
  from those rows, with who or what read them (`human`, `llm:<model>`), how many,
  and when.

**Recall is measured against `data/flora.csv`**, the published FLoRA database and
this project's only gold standard — see [`analysis/gold/README.md`](../../analysis/gold/README.md)
for what a row is, how to refresh it, and the recall monitor it supports. Every
paper in it is a known-good replication, so "how many known FLoRA papers did this
rule discard?" is answerable exactly, for free, at every routing release. No
LLM-labelled corpus may serve as a recall denominator: labels produced by a model
downstream of the keyword filter cannot measure that filter.

Which rules currently discard, and on which level, is recorded in the specs
themselves — each `measured` entry carries its own rationale and its own open
obligations.

## Promoting a rule out of shadow

Five facts set the bar, and each one moves it in a specific direction.

1. **The two errors are not comparable.** A wrongly discarded paper is gone: no
   later stage sees it, and nobody will ever know it was there. A wrongly admitted
   row costs one cheap-screen call, about $0.001–0.002. The bar is therefore about
   how many real papers a rule may lose, and it is not a precision target —
   precision is what we can measure, loss is what we care about.
2. **Recall against `flora.csv` is free and continuous.** The monitor
   ([`analysis/gold/README.md`](../../analysis/gold/README.md)) needs no LLM, no
   sampling and no labelling, and can run at every routing release. Anything free
   should be a hard gate rather than a study.
3. **A sample size follows from the decision it supports**, not from a round
   number. What a clean sample of size *n* buys is an upper bound on the miss
   rate, ≈ 3/*n* at 95% confidence when nothing was missed (the rule of three).
   The bound that matters is on *papers*, so it depends on how many rows the rule
   discards.
4. **A rule that cannot admit needs no evidence at all.** `screen_cheap`,
   `screen_expensive` and `needs_human` rules only reorder spend; the worst
   they do is buy a screen call. `measured: []` on a non-discard rule is correct,
   not an omission, and `validate_spec()` does not ask for more.
5. **Shadow is not automatically the safe setting for an admission rule.** A
   shadow rule cannot outrank anything, so shadowing an admission rule whose
   precedence sits above a live discard removes a guard from the rows that
   discard then wins. Weigh that loss against the admission's own risk — and
   remember the tier gates bound what a live admission can cost: the cheap tier
   defaults to `mode="validation"` and discards nothing until `--live`, and no
   tier spends without `--run`.

### Recipe for a draft `discard` rule

1. **Ship it `"shadow": true`.** It is evaluated and attributed, it changes no
   row's pile, and its would-be discards are visible in `evaluations`.
2. **Run the recall monitor on those would-be discards** (specified in
   [`analysis/gold/README.md`](../../analysis/gold/README.md); not yet built, so
   today this step is a hand count against `data/flora.csv`). Zero known FLoRA
   papers discarded is a *necessary* condition — the monitor is not a random
   sample of what the rule kills, so passing it proves nothing on its own, but
   failing it is decisive. One known FLoRA paper discarded stops the promotion;
   narrow the rule.
3. **Decide which kind of claim the rule makes.**
   - **STRUCTURAL** — the matched token names something that is definitionally not
     a study: a DOI path segment the publisher mints for review objects, a registry
     work type, a data-repository DOI prefix, a molecular-biology term of art. The
     evidence is *an argument plus the external fact it rests on* (which registrant,
     which registry vocabulary, checked when). No sample: the claim is about what
     the token means, and a sample would only re-measure the language. Level
     `trusted`, and the rationale must say why no sample is needed — "we didn't take
     one" is not that reason. Then go to step 5.
   - **STATISTICAL** — the rule bets that a phrase *usually* means something out of
     scope. Only a reading of what it discards can support that. Go to step 4.
4. **Size the sample from the loss you will accept.** Let *V* be the rule's discard
   volume over the population it will run on (count it — that is what the shadow
   evaluations are for), and *L* the number of real replications you are willing to
   lose to this one rule. A clean sample of *n* bounds the loss at about 3*V*/*n*
   papers, so **n ≥ 3V/L**. Draw those *n* rows uniformly at random from the rows
   the rule actually discards in the current pool — not from a convenience set, and
   not from rows selected by any other rule — and read them. `human`, or
   `llm:<model>` where the model reads the paper's own text and its labels come from
   nothing downstream of the rule being tested.
   - **Zero real studies in the sample → promote.** Record `level`, `n`, `date`, the
     population sampled from, and the loss bound the sample buys.
   - **Any real study in the sample → do not promote.** One miss in *n* is a point
     estimate of *V*/*n* papers lost, which for any *V* worth filtering is far past
     any defensible *L*. Narrow the rule and start again.
   - For a high-volume rule, 3*V*/*L* is often an infeasible number of rows to read.
     That is the arithmetic telling the truth: a broad exclusion cannot be justified
     by a sample anyone can afford. The response is to split or narrow the rule until
     *V* is small — a rule with several arms is sampled per arm, and an arm that
     cannot be afforded is an arm that should not discard — not to lower *L*.
   - Set *L* against what the pipeline expects to find, not against *V*. FLoRA holds
     ~1,500 replication papers today; a single keyword rule silently costing more
     than a handful of new ones is not a rule anyone would have agreed to.
5. **Flip `"shadow": false` and add the `measured` entry in the same commit**, so a
   reviewer sees the claim and the promotion together.
6. **The obligation stands after promotion.** The recall check belongs at every
   release; a live rule that starts discarding a known FLoRA paper goes back to
   shadow, and the paper is the bug report.

## `pyre_regex`

A loader-only extension key inside a `match` object, legal only on a match that
is decomposed into `any_of`/`all_of`/`none_of`. It holds the original Python
`re` pattern for a rule whose faithful form needs a lookaround; the decomposition
next to it is what RE2 (and so pyarrow) evaluates, and nothing evaluates the
`pyre_regex` form.

No rule in the current bundle uses it; the archived patterns that did are in
[`rule_ideas.md`](rule_ideas.md), the decomposition and the lookaround original
side by side, which is what the key is for. `validate_spec()` accepts and checks
it, so a rule whose faithful form needs a lookaround can record that form next to
the pattern the engine actually runs.

## `aliases.json`

`{"version": 1, "aliases": {}}` — a flat map from a superseded OpenAlex work id
to its canonical one, both as bare integers written as JSON object keys/values
(`{"2741809807": 4210170740}`). Empty to start. JSON has no comments, so any
provenance for an entry belongs in the commit message that adds it. Aliases
resolve before any state is keyed by `work_id`, and the file's hash is one of
the inputs to the routing release id.

## Known intended divergences from `keyword_verdict()`

Two survive the v2 rewrite, both recorded and reasoned in
`docs/filter-engine.md` §"Known intended divergences": the concept arm routing
instead of killing, and no-abstract rows going to `pending/no_text` instead of
being screened blind. Parity is measured against those intended semantics
(#148), so a parity report that flags one of them is reporting a known
divergence, not a bug.
