# Pre-screen evaluation — findings (issue #130)

Run 2026-08-02 and 2026-08-03. Shipped configuration: prompt `p7` (= `_PRESCREEN_PROMPT`
in `shared/prompts.py`), voters `qwen/qwen3-30b-a3b-instruct-2507` +
`mistralai/mistral-small-24b-instruct-2501` via OpenRouter. Population: the rows of the
eval sets that Stage 2 does not already kill (see `README.md`).

| | the models alone | net of the override |
| --- | --: | --: |
| screen-confirmed negatives discarded (n=184) | 87% | **60%** |
| curated negatives discarded (n=400) | 46% | **7%** |
| gold positives lost (n=567) | 4 | **0** (95% CI 0.00–0.67%) |

Two things to read off this table. The tier loses no gold positive, but only the override
makes that true — the two voters' errors are 37.8× more correlated than independence, so
the AND gate is not the safety mechanism its shape implies. And the two negative buckets
disagree by a factor of eight on what the tier nets, because the override fires on 91% of
one and 36% of the other; the live saving is bracketed, not measured, until the shadow run.

## The question has to be answerable with "no"

This is the finding worth keeping, and it took two rounds to reach.

The issue proposed a minimal, positively framed prompt: *"Could this paper be re-testing
or re-checking a specific earlier study's finding? Answer yes if it might be, no only if
it clearly could not be."* It discarded **1% and 5%** of confirmed negatives. Safe, and
useless.

The reason is not that the models are too weak. It is that the question has no reachable
negative branch. An abstract is partial information, so almost any paper *could* be
re-testing something, and a model answering "yes" is being correct rather than timid. No
amount of insisting fixes that; the question has to change.

What changes it is asking about **evidence** rather than possibility: *"Is there anything
here suggesting that the authors deliberately check a specific result from earlier
published research?"* That is a claim about the text, and a small model can falsify a
claim about the text. Four framings, same rows, single voters:

| prompt | framing | nemo | qwen3-30b | mistral-small |
| --- | --- | --: | --: | --: |
| `p1` | could this **possibly** be one? | 1% | — | — |
| `p8` | which is the **better description**? | 27% | 58% | 54% |
| `p6` | would a reader **describe** it as one? | 52% | 68% | 29% |
| `p5` | any **positive indication** in the text? | 93% | 63% | 61% |
| `p7` | does anything **suggest a deliberate check**? | **97%** | **89%** | **96%** |

(% of the 184 screen-confirmed negatives each voter discards alone.)

The framings that ask about possibility or characterisation discard least; the two that
ask what the text shows discard most, and `p7` is the most consistent across models.
Three details in `p7` carry that, and none is decorative: defining both vocabularies up
front stops "reproduction" being read as ordinary data re-use; "deliberately" excludes
papers that merely build on prior work; and routing genuine ambiguity to "yes"
explicitly is what stops the loosened gate taking the uncertain rows with it.

Through the AND gate on the shipped pair:

| prompt | neg discarded | net of override | positives lost | net of override |
| --- | --: | --: | --: | --: |
| `p8` | 45% | 28% | 0 | 0/567 |
| `p5` | 52% | 38% | 0 | 0/567 |
| **`p7`** | **87%** | **64%** | 4 | **0/567** |

`p7` is the only framing aggressive enough to lose gold positives at all — and all four
it would lose carry an explicit replication phrase, so the override rescues every one.

Every comparative table in this report — across prompts, models and pairings — is
computed on the 184 screen-confirmed negatives with the **pre-widening** override, which
is what keeps the rows comparable to each other. The shipped configuration's current
absolute numbers are the ones in the headline table.

## The model matters as much as the prompt

On identical text (`p4`), single-voter discard rates across the cheap field:

| model | $/1k rows | discard on negatives |
| --- | --: | --: |
| `google/gemma-3-12b-it` | 0.028 | 15% |
| `cohere/command-r7b-12-2024` | 0.021 | 24% |
| `mistralai/mistral-nemo` | 0.010 | 55% |
| `google/gemini-2.5-flash-lite` | 0.056 | 58% |
| `mistralai/mistral-small-24b-instruct-2501` | 0.030 | 67% |
| `qwen/qwen3-30b-a3b-instruct-2507` | 0.030 | 77% |

15% to 77% on the same words. A prompt result from one small model says nothing about
another, and price does not predict behaviour — the most expensive model tested is
mid-table.

**Reasoning models are the trap here, and the headline price hides it.** This task wants
one word back; a model that thinks first pays for the thinking as output tokens. Measured
per call on this prompt: `qwen/qwen3.5-9b` emits **1,832** output tokens, taking a
$0.10/M model to **$0.336 per 1,000 rows — 11× the shipped pair** — and 30% of its
replies still overran a 3,000-token cap. `qwen/qwen3.7-flash` emits ~550 and
`deepseek/deepseek-v4-flash` ~100, against 9 for the models that ship. Judge these on
measured output tokens, never on the advertised input rate.

`inclusionai/ling-2.6-flash` — the cheapest thing here at $0.0055/1k and the issue's
first choice — was unmeasurable on 2026-08-02: its single OpenRouter endpoint (Novita)
returned 429 to every call under every routing mode. It answered normally on 2026-08-03
and has now been run on all four buckets.

| voter | discards, screen-confirmed | discards, curated | gold positives lost alone |
| --- | --: | --: | --: |
| `ling-2.6-flash` | 96% | 82% | 29/567 (5.1%) |
| `qwen3-30b-a3b` | 89% | 49% | 5/567 |
| `mistral-small-24b` | 96% | 82% | 12/567 |

It discards as aggressively as mistral-small at a fifth of the price, and paired with
mistral-small it now loses no gold positive and nets marginally more than the shipped
pair (24.7% vs 23.6% over both negative buckets). It is not shipped for the reason nemo
is not: alone it misses one gold positive in twenty, five times qwen's rate, and the
paper the pair would have lost before the override was widened (`10.1111/j.1469-8986.2010.01022.x`,
"In a replication of previous results") was saved by a pattern added the same day. That
is a configuration whose zero rests on the regex having just been extended, not on either
voter being sound. The shipped pair is two individually sane voters; ling is the obvious
candidate if the tier's cost ever becomes the binding constraint.

### Which pair to run

Every pairing of the three models measured on `p7`, net of the override:

| pair | neg discarded | positives lost | $/pass |
| --- | --: | --: | --: |
| nemo + mistral-small | 66% | 0/567 | $1.28 |
| **qwen + mistral-small** (shipped) | **64%** | **0/567** | **$2.25** |
| nemo + qwen | 62% | 1/567 | $1.28 |

`nemo + mistral-small` scores marginally better and costs $1 less per pass, and it is not
what ships. The reason is what each voter does *alone* on `p7`:

| voter | discards of 184 negatives | gold positives lost alone |
| --- | --: | --: |
| `qwen3-30b-a3b` | 89% | 5/567 |
| `mistral-small-24b` | 96% | 12/567 |
| `mistral-nemo` | 97% | **39/567** |

nemo says "no" to almost everything, including one gold positive in fourteen. Paired with
mistral-small the measured loss is still zero, because the second voter and the override
catch what nemo drops — but the AND gate has then degenerated towards a single-voter gate
with a noisy co-signer, and its safety depends on the other two mechanisms holding. The
shipped pair is two voters that are each individually sane, which degrades more gracefully
if one model's behaviour drifts. $1 per corpus pass is not a reason to give that up.

nemo also proved the least reliable under concurrency here — 429s, occasional null
content, occasional unparseable replies. That costs savings rather than papers, since
every non-answer proceeds, but it makes it a poor voter 1.

## The AND gate is not two independent opinions

The tier's safety story is "both voters have to agree, so a single model's blind spot
cannot lose a paper". Measured over the 567 gold positives, that story is largely false:

| | count | rate |
| --- | --: | --: |
| qwen3-30b misses | 5 | 0.88% |
| mistral-small misses | 12 | 2.12% |
| **both miss the same paper** | **4** | **0.71%** |
| expected if errors were independent | 0.11 | — |

The voters fail together **37.8× more often than independence predicts**. When qwen
misses a gold positive, mistral-small misses the same one 80% of the time, against a
2.1% base rate. Four of qwen's five misses are joint.

That is not surprising in hindsight — the two models see the same prompt, the same
truncated abstract and similar instruction tuning — but it means the AND gate supplies
much less protection than its shape implies, and a second voter cannot be assumed to
catch the first one's failures. **The deterministic override, not the pair, is what
carries the safety**: it caught all four joint misses, and without it the measured loss
would be 4 of 567 rather than 0.

### What the second voter actually buys

Dropping to one model and keeping the override:

| configuration | neg discarded (net) | positives lost (net) | $/pass |
| --- | --: | --: | --: |
| qwen alone + regex | 64% | 1 | $1.48 |
| mistral-small alone + regex | **68%** | 1 | $1.49 |
| ministral-14b alone + regex | 66% | 1 | $5.90 |
| llama-3.1-8b alone + regex | 65% | 2 | $1.49 |
| nemo alone + regex | 66% | 6 | $0.51 |
| **qwen + mistral-small (shipped)** | 64% | **0** | $2.25 |

The second voter's measured contribution is **one paper in 567**, and it *costs* four
percentage points of discard rate and $0.77 a pass. On these rows the ensemble is close
to redundant: one sane model plus the regex gets the same safety to within a single
paper, and mistral-small alone would discard more.

### So does this argue for a different pair, or against the ensemble?

**Not for a different pair.** Every pairing tested lands at zero net loss once the
override runs, and the joint-miss count only moves between 2 and 8 before it. The 37.8×
figure is also partly an artefact of both voters being *good*: low individual error rates
make the expected joint count tiny (0.11), so any overlap at all reads as a large
multiple. Pairs that score a "better" ratio do so because one member is much worse —
`mistral-small + llama-3.2-3b` is 1.5× independent only because llama-3.2-3b misses 219
positives. The ratio flatters bad pairs and should not drive the choice.

**Partly against the ensemble, honestly.** The AND gate is not doing what its shape
claims, and the report should not have implied it was. Two reasons to keep it anyway, and
neither is visible in the table above:

- These are the *easy* positives. The one-paper gap between "pair" and "single model" is
  measured where both models do well; on the marginal, oddly-phrased papers this tier
  actually gates, the models' errors have more room to diverge and the second vote has
  more to catch.
- A second voter is the only defence against one provider's behaviour drifting. Model
  identifiers do not change when serving does, and a silent shift in a single-model gate
  would go straight into terminal discards.

$0.77 a pass is a low price for those two. But the framing changes: **the regex is the
safety mechanism and the second model is insurance**, not the other way round. Invest in
the override's coverage, watch the per-voter "no" rates for drift, and do not expect a
third voter to add much.

## The override is what carries the safety

The four gold positives the AND gate would discard, and the phrase that saves each:

| case | DOI | phrase |
| --- | --- | --- |
| GP314 | 10.1073/pnas.2202700119 | "conceptual replication" |
| GP317 | 10.1075/target.18159.ola | "conceptual replication" |
| GP395 | 10.1089/cap.2024.0078 | "Replication of the" |
| GP527 | 10.1128/jvi.00068-12 | "replication of the" |

So the measured loss with the override on is **0 of 567**. The override also fires on 36%
of the confirmed negatives, and that is the right price: a needless rescue costs one
$0.0018 screen call, a missed one costs a replication study permanently. The gap between
87% and 60% is exactly this.

The safety margin therefore rests on a hand-written regex, and a phrasing it does not
know is the tier's live failure mode. That is what the next two sections are about.

### What the tier actually saves depends on the bucket

Running the 400 curated negatives under `p7` gives a very different net saving from the
screen-confirmed bucket:

| bucket | n | AND-discard | net of override | override fires on |
| --- | --: | --: | --: | --: |
| screen-confirmed negatives | 184 | 87% | **60%** | 36% |
| curated negatives | 400 | 46% | **7%** | **91%** |
| gold positives (FLoRA) | 539 | 1% | 0% | 99% |

The models are not much worse on the curated bucket than the headline suggests — they
still AND-discard 46% of it. What removes the saving is the override: it fires on 91% of
those rows, so 155 of the 183 discards are rescued and never happen. These are the old
pipeline's keyword-harvest false positives — molecular-biology "DNA replication",
engineering "replication of the geometry", papers that merely mention replicating — so
they are *enriched for replication vocabulary by construction*, which is exactly what the
override keys on. The screen-confirmed bucket, drawn from the current pipeline's own
population, carries that vocabulary in 36% of rows.

Two things follow. First, the live net saving is bracketed by 7% and 60%, and where it
lands depends on how much of the real Stage 3 population reads like each bucket — a
question only the shadow run answers. Second: **the override is where the tier's benefit
goes.** At $0.0018 a needless rescue a single pattern is cheap, but on a vocabulary-rich
population the set of them erases most of the saving. Coverage on the positives is bought
directly out of it, which is why the next section prices every proposed pattern against
both.

### Measured on 7,505 papers, it was catching four in five

The override's perfect score above is on the 567 rows that motivated its patterns, so it
was re-measured against every FLoRA replication and reproduction with a usable abstract —
7,505 papers, 13× the derivation set — and 1,333 non-replications that genuinely reach
Stage 3. Full analysis: `OVERRIDE_EVAL.md`.

It fired on **79.9%** of the positives. One FLoRA paper in five states its design in
words the regex did not recognise, so the tier's primary safety mechanism had a hole a
fifth as wide as its coverage. Five of the seventeen patterns contributed zero unique
matches, and the reproduction/re-analysis block turned out to be written from the
vocabulary reproductions *ought* to use: `re-analysis of the original/published` matched
one paper in 7,505.

Sixteen patterns were derived from the missed half of that corpus and reported on a
held-out half they were never shown. They are now shipped in `_SIGNAL_PATTERNS`:

| | override recall on positives | fires on screen-confirmed negatives |
| --- | --: | --: |
| before | 79.9% | 30.6% |
| **after** | **94.7%** (held-out 94.1%) | 36.1% |

The price is ~$7 per corpus pass in screen calls the tier no longer avoids — about a
quarter of what it was saving, and it takes the shipped pair's net discard on the
screen-confirmed bucket from 64% to 60%. That is the trade the tier exists to make in the
right direction: the discard is terminal, so recall on positives is not commensurable
with dollars.

Two boundaries were tested and declined. Widening `replication of` to a list of
study-like objects buys +1.1 points of recall for $6; it is defensible and was left out.
The bare phrase `replication of` buys the remaining 4 points for **$44** — half the
Stage 3 screening budget — because "replication of DNA", "replication of HIV" and
"replication of the model" are the same eleven characters. At that firing rate the
pre-screen would be disabled by regex rather than configured.

What no regex reaches: 43 of the 7,505 positives contain no `replicat*`, `reproduc*` or
`re-analy*` at all. Replications that never say so are the standing argument for keeping
a model in front of the discard.

Stage 2's own verdict cannot serve as the override: 98% of rows reaching Stage 3 carry
`filter_status = replication` at `high` confidence, including **all 184** screen-confirmed
negatives, so bypassing on it would disable the tier entirely.

## What it costs and saves

From the measured 583 input / 9 output tokens per row. Voter 2 is asked only about rows
voter 1 rejects (~52% of the live mix), so voter 1 runs on everything and ordering is a
real cost lever.

| | per 49,800-row pass |
| --- | --: |
| tier cost | $2.25 |
| screen calls avoided (35% of rows) | $30.35 |
| **net** | **~$28** |

Against a ~$87 screening bill — a number not worth taking any risk for on its own; the
case for the tier is #129.

That row assumes the live mix behaves like the screen-confirmed bucket. Scaling by the
curated bucket's net rate instead (7% rather than 60%) the tier avoids ~4% of rows, saves
~$3.50 and nets about **$1** a pass — it pays for itself and no more. The true figure is
somewhere between $1 and $28, and the shadow run measures it directly on the real
population.

## What this evaluation cannot tell you

**Zero observed misses is not a bounded miss rate.** The 95% interval on 0/567 still
reaches 0.67%. Bounding the true rate below 0.5% needs ~600 gold positives with zero
misses — about what is here — but that is one-sided comfort only if the sample is
representative, and it is not:

- **The gold positives are the easy positives.** Canonical, well-described replications
  already in FLoRA. The marginal, oddly-phrased papers that only keyword search finds are
  what a small model misses, and they are structurally absent.
- **The negatives are labelled by the system under test.** The 184 "screen-confirmed"
  negatives carry the validated screen's own error rate, so 60% is agreement with the big
  screen, not accuracy against truth.
- **These rows are derivation data.** They are the population that motivated the issue,
  and `p7` was chosen on them. The effect size (87% vs 1%) is far too large for a
  winner's curse to explain, but the exact rates are in-sample.

**Coverage gaps.** `p6` and `p3` were not run on the positives, having been ruled out on
the negative side. The curated-negative bucket has now been run under `p7`, and it did
not confirm the headline — it bracketed it (above).

## Rejected: shortening the input

Input is 583 of the ~592 tokens per row, so truncating the abstract is the only real cost
lever. Cutting it from 3,000 to 700 characters saves ~45% of input and costs far more
than it saves:

| model | in/call | negatives | gold positives lost |
| --- | --: | --: | --: |
| qwen3-30b @ 3000 | 583 | 89% | 0.9% |
| qwen3-30b @ 700 | 310 | 91% | **3.8%** |
| mistral-small @ 3000 | 734 | 96% | 2.0% |
| mistral-small @ 700 | 465 | 95% | **6.5%** |

Roughly a 3× increase in positive loss for $1 per corpus pass. The evidence that a paper
re-tests something is not reliably in the opening sentences, and the papers where it is
not are exactly the ones this tier must not lose.

## Recommendation

The tier ships **on** (`PRESCREEN_ENABLED=1`): #129 makes the snapshot scan the only path
into Stage 3, and that population cannot be screened at full price. The ~$28 at today's
volume was never the argument.

`PRESCREEN_MODE=shadow` records every verdict and acts on none. It exists because one
question remains unanswered by any evidence here: how often the tier would discard a row
the validated screen goes on to keep. That is an incremental loss rather than a miss
against a gold list, it needs no gold labels, and it is measurable in the thousands on
the live population. Running the first pass of the new corpus in shadow costs the tier's
own $0.045/1,000 rows and settles the question that the gold-positive sets structurally
cannot.

Two things to watch once it is discarding, both following from the correlated-failure
result above:

- **Sample `data/prescreen_discard.csv` by hand, regularly.** Nothing else in the
  pipeline ever looks at those rows again, and agreement between two correlated models is
  not evidence they were right.
- **Treat the regex as load-bearing.** It is what caught every joint miss. A phrase it
  does not know is the tier's live failure mode, and any edit to it should be followed by
  `--rescreen` over the discards so a widened override reopens what a narrower one lost.
