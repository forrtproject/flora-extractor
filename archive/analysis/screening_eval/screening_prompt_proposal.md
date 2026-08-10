# Screening prompt — proposal, settled rules, open questions

Input: `flora_coding_75_results.csv` (75 hand-coded cases), joined to the case texts
in `coding_sheet_75.csv`. Revised after the 2026-07-31 rule discussion.

---

## 1. Settled rules

1. **Intent.** Replication or reproduction must be an *aim* of the paper. A re-test that is
   incidental — reported as a result, or as the interpretation of a result — does not qualify.
2. **Self-replication counts** when it is a separate paper re-testing the authors' own earlier
   published finding. (An internal replication *within* one paper — "Study 2 replicates Study 1" —
   remains a Stage-2 false positive.)
3. **Instrument validation: only *initial* validation is excluded.** Re-validating, re-testing or
   evaluating the reproducibility of an already-published instrument, measure, test or procedure
   counts. A new language is a new context, exactly like lab-to-field.
4. **Technical/measurement reproducibility is not replication** when there is no intent to
   re-check a finding — sample-to-sample or device-to-device precision, inter-rater agreement.
5. **Tool benchmarking is out**: a new simulation, model or method shown to reproduce known
   results in order to demonstrate that the tool works. A paper *using* such a tool to check a
   published result is in.
6. **The target must be a particular reported finding**, not established background knowledge.
   Research that builds on, applies or extends what the literature already holds — "known
   polymorphisms", "the well-established association between X and Y" — is out, even when the
   paper tests whether that knowledge holds in its own sample. The source study need not be
   named; what must be identifiable is a specific claim someone reported.

## 2. Re-coded cases

| Case | Paper | Was | Now | Reason |
| --- | --- | --- | --- | --- |
| 11 | PDRP fMRI validation study | no | **yes** | Re-validation of an already-published topography (rule 3) |
| 37 | Japanese ecSI-2.0 translation | no | **yes** | New language = new context (rule 3) |
| 50 | Finger test for NSTI | no | **yes** | Evaluates a test Andreasen proposed (rule 3) |
| 52 | SWAT irrigation-controller protocol | no | **yes** | Independent reproducibility of an existing protocol (rule 3) |
| 55 | Hybrid Russe procedure | no | **yes** | Checks a reported 100% union rate (rule 3) |
| 56 | Dual Frequency Head Maps | unclear | **yes** | Already lab-validated; now validated in the field (rule 3) |
| 57 | Acromion index | no | **yes** | "verify whether the AI is a useful index" — checks an advocated claim (rule 3) |
| 1 | iPSC neuron resource paper | no | no | Re-test is incidental to a characterisation paper (rule 1) |
| 2 | Quantum Hall resistance standard | no | no | Device sample-to-sample precision, no intent to re-check (rule 4) |
| 12 | Sleep-deprivation connectivity states | no | no | **My reading — please confirm.** See §3 |
| 23 | AOSpine classification reliability | no | no | **My reading** — first reliability evaluation of a newly proposed system = initial validation |
| 5 | Use case metrics | no | no | Two experiments replicating *each other* — internal replication |
| 40 | Tumor-stroma ratio protocol | no | no | Protocol to apply the authors' own method prospectively; no prior claim tested |

## 3. What the re-coding does to the findings

**The two headline conclusions from the first pass both weaken.**

| Block | n | no | yes |
| --- | --- | --- | --- |
| A · calibration | 15 | 12 | 3 |
| B · discard path | 22 | 21 | **1** |
| C · disagreements | 23 | 17 | **6** |

- **The discard path is 21/22, not 22/22.** Case 37 (the Japanese translation) is a real
  replication that the screen discarded. One miss in 22 is still decent, but the path is no
  longer clean, and the miss is a *systematic* kind — an instrument re-validation, exactly the
  category rule 3 just widened.
- **The disagreement pile must NOT be bulk-resolved as negatives.** 6 of 23 are real
  replications under the settled rules. Reviewing those 125 rows by hand, or re-screening them
  with the corrected prompt, is now the right call — my earlier "bulk-resolve as no" advice was
  an artefact of the pre-discussion coding.
- **Positives rise from 3 to 10 of 60.** Still too few to split, but enough to be worth
  something as hard positives — and they are hard in the useful way, since 7 of the 10 come from
  the discard and disagreement pools rather than from obviously-labelled replications.

### Case 12 — does it show intent? My reading: no

> "Robustly linking dynamic functional connectivity states to behaviour is an important goal…
> **Previously, using a sliding window approach, we identified two dynamic connectivity states
> linked to arousal. Here, in an independent dataset,** 32 healthy participants underwent two
> sets of resting-state fMRI scans… clustering analysis revealed five centroids that **were
> highly correlated with those found in previous work**… **Our results provide good evidence of
> the validity and reproducibility of DFC measures.**"

Nothing states an aim of re-checking the earlier finding. The stated goal is linking connectivity
states to behaviour; the re-identification of the centroids happens along the way, and the
reproducibility claim appears in the conclusion as an *interpretation of the results* — which is
the exact phrasing rule 1 excludes. So: no, and the panel was wrong.

The counter-argument is that "Here, in an independent dataset" is deliberate design, and the
title's "robustly" signals it. If you read that as intent, the case flips and rule 1 needs a
sentence saying that a deliberate independent-dataset design counts as intent even when the
paper does not use replication vocabulary.

---

## 4. Proposed prompt

```
You are classifying papers for a replication database.

A paper qualifies when checking a specific finding from earlier research is one of its
stated aims:
  REPLICATION  — it collects new data to re-test a finding reported in a previously
                 published study.
  REPRODUCTION — it re-analyses that study's data to check the reported result.

Re-testing the same claim in a different population, country, language or setting counts,
and so does a conceptual replication that re-tests the claim with a changed method, measure
or paradigm. It also counts when the authors are re-testing their own earlier published
finding in a separate paper.

The check must be a purpose of the paper. A finding that merely turns out to agree with
earlier work — reported as a result, or as an interpretation of the results — does not
qualify, however clearly it is stated.

The target must be a particular finding that someone reported, not established background
knowledge. A study that tests whether what the literature already holds — "known
polymorphisms", "the well-established association between X and Y" — applies in its own
sample is ordinary research building on prior work, and does not qualify. The source study
does not have to be named: what matters is whether a specific reported claim is being
checked, or whether the paper is working from a body of accepted knowledge.

Instruments and procedures: re-testing, re-validating or evaluating the reproducibility of
an ALREADY PUBLISHED instrument, scale, test or clinical procedure qualifies, including in
a new population, language or setting. Only the FIRST validation of a newly proposed
instrument does not qualify, because there is no earlier finding to check.

When the authors explicitly describe their own study as a replication or reproduction,
accept that framing and answer "yes". Do not second-guess a self-declared replication.

A comment, reply or letter that presents its own re-analysis of a published result
qualifies. A comment that only argues about an earlier study, with no new data or
analysis, does not.

The following do NOT qualify, even though they use the same vocabulary:
  1. Technical and measurement reproducibility with no finding at stake — sample-to-sample
     or device-to-device precision, inter-rater or test-retest agreement reported for its
     own sake.
  2. Tool benchmarking — a new model, simulation or numerical method shown to reproduce
     known results in order to demonstrate that the tool works. (A study that USES such a
     tool to check a published result does qualify.)
  3. The ordinary-language, biological and field-specific senses — DNA, viral or cell
     replication; "replication" as a count of overlapping samples in a chronology;
     "replicated across sites" describing a study design.
  4. Papers about replication — meta-analyses, reviews, commentary on the replication
     crisis, or a stated need for future replication.
  5. Internal replication — a paper whose Study 2 replicates its own Study 1.

Answer:
  yes     — the abstract shows that re-testing a published finding, or re-analysing an
            earlier study's data, is an aim of the paper
  no      — the abstract shows that it is not
  unclear — the abstract does not settle it either way

A high-confidence "no" discards the paper, so use high confidence only when the abstract
clearly describes a purpose that does not qualify. An abstract that describes checking a
specific reported finding but does not name the study is NOT grounds for a high-confidence
"no" — say "yes" and leave the source to be identified later.

Also return a review_flag naming the pattern this paper fits, from this list:
  confident_replication  — explicitly framed as a replication or reproduction
  self_replication       — the authors re-test their own earlier published finding
  measurement_validation — re-validation of a published instrument, test or procedure
  context_transfer       — the same claim re-tested in a new population, language or setting
  incidental_finding     — a re-test is present but is not an aim of the paper
  initial_validation     — first validation of a newly proposed instrument
  tool_benchmark         — a new method shown to reproduce known results
  builds_on_literature   — tests established knowledge rather than a particular reported claim
  terminology_only       — the word is used in a biological, ordinary or field-specific sense
  about_replication      — a review, meta-analysis or commentary on replication
  other

TITLE: {title}

ABSTRACT: {abstract}

Respond with ONLY this JSON — no prose outside the braces:
{"is_replication": "<yes|no|unclear>", "confidence": "<high|medium|low>",
 "review_flag": "<one value from the list>",
 "evidence_quote": "<exact short quote from the abstract, or empty>",
 "reasoning": "<one sentence>"}
```

### The `review_flag` column

One new enum column carried from the screen into `extracted.csv` and on into the validation DB,
so a validator sees *why* the row is here and the boundary cases are findable as a group. Three
things it buys beyond validator convenience:

- **Re-codable evaluation data.** Once flags exist, "how does the screen do on measurement
  validations?" is a query, not another hand-coding exercise.
- **A monitorable discard path.** The one miss in block B was a `measurement_validation`. If
  discards are broken down by flag, that class of miss shows up as a rate instead of needing a
  22-case audit to notice.
- **Cheap re-adjudication when a rule changes.** This discussion re-coded 7 of 60 cases; with
  flags, the equivalent sweep over the full corpus is a filter.

Cost: one field in the JSON, one column in `shared/schema.py` / `docs/csv-schema.md` /
`misc/sample_extracted.csv`, and a pass through `csv_to_db` so validators actually see it. Small,
and it fits the same slot as the `classify_llm_model` column added in PR #96.

---

## 5. Remaining open questions

### SETTLED · Target specificity — case 42 is a no
The three cases that looked like a specificity problem turn out to be three different problems,
and only one of them is about specificity:

- **Case 49 (Basque dendrochronology)** — "significantly improves **the replication of this
  master chronology**" is field jargon for the number of overlapping tree-ring samples per year.
  Not a replication in any sense. Handled by exclusion 3 in the draft, which now names it.
- **Case 48 (TENS simulation environment)** — "allowing the replication of already reported
  experimental findings" is a capability claim for a new simulation tool. Handled by exclusion 2.
- **Case 42 (POLG1 in HIV-infected Zulu patients)** — the genuine case:

  > "Objective. To determine whether **known monogenic POLG1 polymorphisms** could be linked with
  > the unexpectedly high incidence of SHL/LA observed in HIV-infected Zulu-speaking patients…
  > Conclusion. This study has shown that our sample of the Zulu-speaking population **does not
  > exhibit** a genetic predisposition to SHL/LA associated with known monogenic POLG1 mutations."

  This *is* a stated aim of testing a previously reported association in a new population — a
  conceptual replication by rule 3's logic — and it reports a failure to replicate. What it lacks
  is any identifiable source: "known monogenic POLG1 polymorphisms" points at a literature, not a
  study. (Note the abstract also contains "replication of mitochondrial DNA", the biological
  sense, which is probably what put it in the corpus.)

  **Decision: out.** A clear bar is needed against general research building on established
  findings, which is a far larger population than replications and would swamp the pipeline.

  This is the one place where two clauses in the prompt genuinely pull against each other, so
  the wording separates them explicitly: the target-specificity clause excludes papers working
  from *accepted knowledge*, while the confidence rule still forbids a high-confidence "no"
  merely because a *specific* finding's source is unnamed. Case 42 is excluded by the first
  ("known monogenic POLG1 polymorphisms" is a literature, not a claim); a paper saying "we
  re-test the reported effect of X on Y" without a citation is protected by the second.
  This pair is the single most important thing to watch in the evaluation — if the models
  over-apply the first clause, the miss rate on unnamed-but-specific targets will rise, and the
  block-B discard miss (case 37) shows that class of error is already the live risk.

### SETTLED · Replies and comments — in when they re-analyse
**Case 4** (a Reply rebutting a critique, presenting 10 new examples and re-analysing an
independent lab's data) qualifies. A comment that only argues about an earlier study, with no
new data or analysis of its own, does not. Clause added to the prompt.

### SETTLED · `descriptive` — self-declared replications pass
A paper that explicitly calls itself a replication passes the screen on the authors' framing
alone; the screen does not relitigate a self-description. Whether it turns out to be
`descriptive` is then outcome coding's judgment, not the screen's. A paper that reuses methods
without describing itself as a replication still fails the intent bar and is discarded.

This is a meaningful widening: author framing becomes sufficient intent, overriding the
"incidental" test for self-declared cases only. Case 44 ("using 16 elements of replication of
Ramirez and Gordillo's research") stays out — it describes borrowing a framework, not conducting
a replication.

---

## 6. Evaluation design

**Positives come from FLoRA, not from more hand-coding.** `data/flora_entry_sheet.csv` holds
3,802 curated entries, 3,274 with a usable `abstract_r`, of which 1,397 are human-validated.
These are known true positives, free. A few hundred of them measure the miss rate — the
safety-critical direction, since a discard is irreversible and a false positive is only
expensive. Caveat: FLoRA entries are enriched for explicit, well-labelled replications, so they
measure the easy half of sensitivity. The 10 hand-coded positives cover the hard half.

**The 50 hand-coded negatives are the specificity benchmark.** Drawn from the discard and
disagreement pools, so enriched for hard negatives — far more informative than a random draw.
Report sensitivity and specificity separately; a single accuracy number over a set that is 83%
negative will be dominated by specificity and will flatter every prompt.

**Then run the grid once:** {this prompt, production prompt, Q7's rejected rewrite} ×
{candidate voter pairs} × {split vs combined call}, scored on misses first, correct discards
second. The whole hand-coded set is now derivation data, so the honest report is cross-validated
or held out on FLoRA positives — not a clean out-of-sample number. Worth stating plainly wherever
this gets written up.
