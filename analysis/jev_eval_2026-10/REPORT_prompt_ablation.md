# Span-02's decision prompt: error patterns and three variants (2026-10-28)

**No variant improves on the decision prompt this branch ships.** The repeated errors
are mostly not prompt errors: they are title-only records the model calls `unclear`,
which Luna keeps anyway, and a block of benchmark negatives whose labels predate the
measurement re-validation rule. Spend: $0.15 (`score_variants.py` →
`score_variants_out.txt`).

## Method

To keep the prompt from being fitted to these cases, the 2,186 were split in half by a
hash of the case id (even md5 → dev) before any error was read: dev 720 positives and
345 negatives, test 736 and 385. Patterns were read on dev only; the variants were
written from them, run on every case, and judged on test. Negatives are reported by who
labelled them: 161 by people (validators' rejections, the hand-coded screening set) and
569 by earlier pipeline steps (earlier screen discards, an older pipeline's LLM labels).

## What the dev errors were

Of the 26 dev positives Span-02 scored below 0.5, **16 put most of their probability on
`unclear`**, with P(none) near 0: nine have no abstract or a non-abstract ("International
audience", a database notice) under a title such as "A replication of Martin (1987)",
and the other seven announce a replication in the title (four as "replication and
extension"). Luna calls all sixteen qualifying, so the gate keeps them. Discarding on P(none) instead of a low
P(qualifies) therefore changes nothing at the gate. The other ten are genuine `none`
answers: re-tests of published instruments (four), a self-replication, a post-GWAS
replication of an earlier GWAS, and four borderline cases (a replicated literature
review, a "converging evidence" study, and two whose labels are themselves doubtful).

Of the 20 dev negatives Luna calls `none` but Span-02 keeps, three are about a
replication without being one (a PCI review, a commentary, a rejoinder), three are
models or measurement work reproducing known results, one or two are replications inside
the same paper, and the rest are ordinary studies that build on earlier work.

**Measurement re-validation is the largest pattern, and it is a labelling conflict.**
Rule 4 of the screening prompt counts translating, adapting or re-validating a published
instrument as a replication. 78 positives use such wording, and Luna keeps 77 of them.
Of the 55 negatives that do, only 6 were labelled by people; 25 are earlier-screen
discards, of which Luna keeps 19. Those labels predate the rule and should be re-read
before they are used to tune anything.

## Variants

Each was run with Span-02 on all 2,186 cases; the instructions and rules are otherwise
the shipped ones.

- **v2**: a longer `replication` criterion listing the qualifying kinds (with extensions,
  conceptual, new setting, self re-test, instrument re-test); a `none` criterion that adds
  tools reproducing known results, peer reviews, replies and rejoinders, and the
  exceptions for earlier-reported associations and published instruments; and a line
  telling the model to judge a missing abstract from the title.
- **v3**: v2's `none` criterion and title line, the shipped `replication` criterion.
- **v4**: the title line only.

Beside Luna at a 0.7 cut-off (positives lost; human / pipeline-labelled negatives
discarded):

| prompt | dev | test |
|---|---|---|
| *DeepSeek + Luna (production)* | *4 lost, 58% / 83%* | *4 lost, 78% / 86%* |
| shipped | 2 lost, 58% / 82% | 3 lost, 76% / 80% |
| v2 | 1 lost, 55% / 75% | 2 lost, 69% / 75% |
| v3 | 2 lost, 57% / 78% | 4 lost, 73% / 77% |
| v4 | 3 lost, 59% / 81% | 4 lost, 76% / 82% |

v2 moves along the trade-off rather than improving it: on test, one fewer miss (two
overall) for 7 and 5 points less discard. Its long `replication` criterion made the model call
studies that merely build on earlier work replications (21 dev flips, almost all
ordinary studies). v3 and v4 are no better on dev and lose a positive on test. The title
line does what it targets — positives scored mostly `unclear` fall from 40 to 15 — but
that changes no gate decision, and type accuracy is unchanged within a case or two.

## Limits

One run per variant; the dev and test halves each hold about 80 human-labelled
negatives, so a difference of 3 points is two papers; the gate's positive side rests on
the 17 positives Luna misses.
