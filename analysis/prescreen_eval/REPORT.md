# Pre-screen evaluation — findings (issue #130)

Run 2026-08-02. Shipped configuration: prompt `p7` (= `_PRESCREEN_PROMPT` in
`shared/prompts.py`), voters `qwen/qwen3-30b-a3b-instruct-2507` +
`mistralai/mistral-small-24b-instruct-2501` via OpenRouter. Population: the 751 rows of
the eval sets that Stage 2 does not already kill (see `README.md`).

| | measured | 95% CI |
| --- | --: | --: |
| screen-confirmed negatives discarded (n=184) | 87% | 81–91% |
| …after the hard-signal override rescues 43 of them | **64%** | 56–70% |
| gold positives lost (n=567) | **0** | 0.00–0.67% |

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

Two practical traps. `qwen/qwen3.7-flash` emits ~550 output tokens per call, which makes
it one of the dearer options despite a cheap headline rate. `inclusionai/ling-2.6-flash`
— the cheapest thing here at $0.0055/1k and the issue's first choice — has a single
OpenRouter endpoint (Novita) that returned 429 for every call under every routing mode,
with credit on the account. Worth re-measuring if that clears; it is one env var.

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

Two consequences worth holding on to. Adding a third cheap voter would buy less than the
arithmetic suggests. And the regex's coverage is the thing to invest in and to watch,
because it is load-bearing rather than a backstop.

## The override is what carries the safety

The four gold positives the AND gate would discard, and the phrase that saves each:

| case | DOI | phrase |
| --- | --- | --- |
| GP314 | 10.1073/pnas.2202700119 | "conceptual replication" |
| GP317 | 10.1075/target.18159.ola | "conceptual replication" |
| GP395 | 10.1089/cap.2024.0078 | "Replication of the" |
| GP527 | 10.1128/jvi.00068-12 | "replication of the" |

So the measured loss with the override on is **0 of 567**. The override also fires on 31%
of the confirmed negatives, and that is the right price: a needless rescue costs one
$0.0018 screen call, a missed one costs a replication study permanently. The gap between
87% and 64% is exactly this.

It also means the safety margin now rests more heavily on a hand-written regex than it
did under a milder prompt — 43 rescues rather than 27. A phrasing the regex does not know
is the tier's live failure mode.

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
| screen calls avoided (37% of rows) | $32.35 |
| **net** | **~$30** |

Against a ~$87 screening bill. Better than the ~$15 of the first configuration, and still
not a number worth taking any risk for on its own — the case for the tier is #129.

## What this evaluation cannot tell you

**Zero observed misses is not a bounded miss rate.** The 95% interval on 0/567 still
reaches 0.67%. Bounding the true rate below 0.5% needs ~600 gold positives with zero
misses — about what is here — but that is one-sided comfort only if the sample is
representative, and it is not:

- **The gold positives are the easy positives.** Canonical, well-described replications
  already in FLoRA. The marginal, oddly-phrased papers that only keyword search finds are
  what a small model misses, and they are structurally absent.
- **The negatives are labelled by the system under test.** The 184 "screen-confirmed"
  negatives carry the validated screen's own error rate, so 64% is agreement with the big
  screen, not accuracy against truth.
- **These rows are derivation data.** They are the population that motivated the issue,
  and `p7` was chosen on them. The effect size (87% vs 1%) is far too large for a
  winner's curse to explain, but the exact rates are in-sample.

**Coverage gaps.** The curated-negative bucket was not re-run under `p7`, so the benefit
rests on one negative bucket. `p6` and `p3` were not run on the positives, having been
ruled out on the negative side.

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
into Stage 3, and that population cannot be screened at full price. The $30 at today's
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
