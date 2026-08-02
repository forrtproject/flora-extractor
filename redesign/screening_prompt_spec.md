# Task Specification — Replication/Reproduction Screening Step

This document specifies what an LLM prompt for the FLoRA screening step must
accomplish and convey. It is a specification, not a prompt: it fixes the task,
the inputs, the output schema, the decision rules, and a small number of
structural requirements for the eventual prompt. Wording and style are left to
the prompt writer.

**Current version: v3.3** — `_CLASSIFY_PROMPT` in `shared/prompts.py`, evaluated
copy `analysis/screening_eval/prompt_v33.txt`, evidence
`analysis/screening_eval/report_v33.md`. Two things changed since the version
this document was first written against:

- **v3.2** replaced the three-level `confidence` field with a binary `confident`
  (sections 3 and 4.5 below carry the binary field).
- **v3.3** adds the partial-overlap rule (section 4.2, item 7): sharing
  observations with the original does not disqualify a re-test. This aligns the
  screen with FLoRA, which allows a replication to overlap the original's data —
  the "Overlapping observations" decision in the redesign document's section 5.

---

## 1. Purpose and stakes

The screen is the front door of a pipeline that builds a database of
replication and reproduction studies. It reads one paper's title and abstract
and decides whether that paper belongs in the database.

Two properties of the surrounding system drive every rule below:

- **A confident "does not belong" verdict is final.** The paper is discarded and
  never looked at again. Wrongly discarding a genuine replication is the
  expensive error; wrongly passing a non-replication forward is cheap, because
  later stages and human reviewers can still remove it.
- **The screen never has to identify the target study.** A later pipeline stage
  resolves which earlier paper is being checked. The screen's only job is to
  judge whether the paper is the *kind* of study the database collects.

The prompt must make both points explicit to the model, because both change how
borderline cases should be resolved.

---

## 2. Inputs

The prompt receives exactly two pieces of text, supplied through placeholders:

- `{title}` — the paper's title
- `{abstract}` — the paper's abstract

No full text, no reference list, no metadata. The prompt must tell the model to
judge only from this material and not to speculate about content the abstract
does not contain.

---

## 3. Output schema

The response must be a single JSON object and nothing else — no prose before or
after, no code fences, no commentary. Exactly these five fields:

| Field | Allowed values | Meaning |
| --- | --- | --- |
| `classification` | `replication`, `reproduction`, `both`, `none`, `unclear` | What the paper is. Use `both` when the paper both re-analyses earlier data and collects new data to re-test the finding. Use `unclear` when the abstract genuinely does not settle the question either way. Use `none` when the paper does not qualify. |
| `confident` | `true`, `false` | Whether the model would stake the `classification` on the abstract as written, governed by section 4.5. |
| `categories` | JSON array of one or more values from the list below | Every pattern the paper fits, in list order. |
| `evidence_quote` | short exact quote from the title or abstract, or an empty string | The wording that drove the decision. Must be copied verbatim from the title or abstract; empty if nothing supports a quote. |
| `reasoning` | one sentence | Why this classification follows. |

### 3.1 `categories` values

One or more of (multi-select — a declared self-retest carries both `clearly_declared`
and `self_retest`; the array preserves the list order below):

| Value | Gloss |
| --- | --- |
| `clearly_declared` | The authors themselves frame the work as a replication or reproduction. |
| `self_retest` | The authors re-test a finding from their own earlier published study. |
| `measurement_validation` | Re-validation or re-evaluation of an already published instrument, test or procedure. |
| `context_transfer` | The same claim is re-tested in a new population, country, language or setting. |
| `incidental_finding` | A re-test is present in the paper but is not one of its aims. |
| `initial_validation` | The first validation of a newly proposed instrument. |
| `tool_benchmark` | A new method, model or simulation is shown to reproduce known results in order to demonstrate that it works. |
| `builds_on_literature` | The study tests established background knowledge rather than a particular reported claim. |
| `terminology_only` | The vocabulary appears in a biological, ordinary-language or field-specific sense. |
| `about_replication` | A review of, or commentary about, replication itself. |
| `other` | None of the above fits. |

Naming constraint the prompt must respect: within this schema, the words
"replication" and "reproduction" always carry the qualifying sense defined in
section 4.1. Category names must never be presented as using those words in any
other sense.

---

## 4. Decision rules

### 4.1 Core criterion

A paper qualifies when **checking a specific finding from earlier published
research is one of its stated aims.** Two qualifying forms:

- **Replication** — new data are collected in order to re-test a finding
  reported in a previously published study. Code such a paper as
  `replication`.
- **Reproduction** — an earlier study's own data are re-analysed in order to
  check the result that was reported from them. Code such a paper as
  `reproduction`.

A paper that does both — re-analysing existing data *and* collecting new data to
re-test the same finding — is coded as `both`.

### 4.2 Qualifying variants

Each of the following qualifies and must be spelled out in the prompt, because
each is a case a naive reading might reject:

1. **Context transfer.** Re-testing the same claim in a different population,
   country, language or setting qualifies.
2. **Conceptual replication.** Re-testing the claim with a changed method,
   measure or paradigm qualifies.
3. **Self re-test.** Authors re-testing their own earlier *published* finding in
   a separate paper qualifies.
4. **Measurement re-validation.** Re-testing, re-validating or evaluating the
   reproducibility of an **already published** instrument, scale, test or
   clinical procedure qualifies — including when this is done in a new
   population, language or setting.
5. **Comment or reply with its own analysis.** A comment, reply or letter that
   presents its own re-analysis of a published result qualifies.
6. **Author self-declaration.** When the authors explicitly describe their study
   as a replication or reproduction, accept that framing rather than
   second-guessing it, and classify the paper as the type they declare.
7. **Partial data overlap.** Sharing some observations with the original does not
   disqualify: a re-test that extends the original sample, adds a later wave, or
   partially overlaps the original data still qualifies when checking the earlier
   finding is an aim of the paper.

### 4.3 Exclusions

Exclude a paper — that is, return `none` — in the following situations.

**Declared intent.** The paper must set out to check the earlier finding — the
check must be something the paper aims to do, not a by-product. Exclude when the
abstract presents agreement with earlier work as an incidental result or an
interpretive remark rather than an aim. The model should *not* be asked to prove
that re-testing is central to the paper, because centrality cannot be judged
reliably from an abstract — an aim stated anywhere is enough.

**Target specificity.** The thing being checked must be a particular finding
that someone reported, not the accepted background knowledge of a field. A study
that tests whether something the literature already holds — "known
polymorphisms", "the well-established association between X and Y" — applies in
its own sample is ordinary research building on prior work, and does not
qualify. The source study does **not** have to be named; what matters is whether
a specific reported claim is being checked as opposed to a body of accepted
knowledge being applied.

**First validation of a new instrument.** The initial validation of a newly
proposed instrument does not qualify, because there is no earlier reported
finding to check. (Contrast with 4.2 item 4.)

**Comment without analysis.** A comment or letter that only argues about an
earlier study, presenting no new data and no re-analysis, does not qualify.

**Internal replication.** A paper whose Study 2 replicates its own Study 1 does
not qualify; the target must be earlier *published* research by some paper other
than this one.

**Non-qualifying senses of the vocabulary.** The words "replication",
"reproduce", "reproducibility" and their relatives are used in several senses
that must not trigger a qualifying verdict:

1. *Technical and measurement reproducibility* — sample-to-sample or
   device-to-device precision, inter-rater agreement, test–retest agreement.
   Exception: if the paper's aim is to replicate such estimates *across papers*,
   it qualifies.
2. *Tool benchmarking* — a new model, simulation or numerical method
   demonstrated to reproduce known results in order to show that the tool works.
   Exception: a study that *uses* such a tool to check a published result does
   qualify.
3. *Ordinary-language, biological and field-specific senses* — DNA, viral or
   cell replication; "replication" as a count of overlapping samples in a
   chronology; "replicated across sites" describing the internal design of the
   study being reported.
4. *Papers about replication* — reviews, commentary on the replication crisis,
   or a paper that merely states that future replication is needed.
   Meta-analyses are deliberately not part of this exclusion, but the prompt
   does not single them out either way.

### 4.4 Interaction of the rules

The qualifying variants in 4.2 and the exclusions in 4.3 are stated at the same
level; where an abstract could be read either way, the calibration rule in 4.5
decides how confidently the model may act.

### 4.5 Confidence calibration

Because `classification: none` at `confident: true` permanently discards the
paper, the prompt must impose an asymmetric standard:

- Answer **`confident: true` on `none` only when the abstract clearly describes a
  purpose that does not qualify** — for example an unambiguous instance of one
  of the non-qualifying senses, a first validation, or a plainly incidental
  agreement with prior work.
- An abstract that describes checking a specific reported finding **but does not
  name the source study is not grounds for a confident `none`.** Such a paper
  should be classified as qualifying, with source identification left to a later
  pipeline stage.
- When the abstract genuinely does not settle the question in either direction,
  use `unclear` rather than forcing `none`.
- Answer `confident: true` generally for cases the abstract states plainly;
  answer `confident: false` when the judgement rests on inference, or when a
  different reading of the same sentences would change the answer.

---

## 5. Requirements for the prompt's form

These are structural requirements on the artifact the writer produces, not on
its wording:

1. **Task and output schema come first.** The prompt must open by stating what
   the model is deciding and giving the complete JSON schema — field names,
   allowed values, and the `categories` list with its glosses — before any of the
   detailed decision rules. The rules follow the schema, not the reverse. The
   schema must be presented as a field-by-field description plus one *valid*
   example JSON object — never as pseudo-JSON with unquoted unions or
   descriptions, which small models imitate.
2. **Definitions are phrased as coding instructions.** Replication and
   reproduction must be defined in the form "code it as `replication` when …",
   "code it as `reproduction` when …", so that each definition names the schema
   value it produces.
3. **JSON-only response.** The prompt must require a single JSON object as the
   entire response, with no surrounding text, explanation or formatting.
4. **Placeholders.** The prompt must consume the paper via the `{title}` and
   `{abstract}` placeholders.
5. **State the stakes.** The prompt must tell the model that a confident
   `none` discards the paper permanently and that the target study is identified
   later, since both facts are load-bearing for section 4.5.
6. **Every rule in section 4 must appear.** Grouping, ordering within the rules
   section, examples and phrasing are the writer's choice; omission is not.
