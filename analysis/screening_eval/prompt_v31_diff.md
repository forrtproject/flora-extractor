# prompt_v3 → prompt_v31 — diff and rationale

`prompt_v31.txt` is `prompt_v3.txt` with six edits. Everything else is byte-identical:
the JSON schema, the five fields, the category list, the example object, the STAKES
section, qualifying rules 1/2/3/5, the other four WHAT-DOES-NOT-QUALIFY bullets,
non-qualifying sense 4, and the whole CONFIDENCE section are untouched.

Applied: the policy ruling on the instrument boundary, plus §5 drafts F4, F1, F3a, F6
and F2. **Not applied: F5 and F3-full.**

## Change list

1. **Qualifying rule 4 — instrument-boundary ruling (qualifying half).**
   Added "translating or adapting" to the list of qualifying acts, and a clause stating
   that the reliability, test-retest, inter-rater agreement or reproducibility of an
   already published instrument in a new population, language or setting qualifies.
   *Motivating pattern:* P5 (translation / adaptation of a published instrument) and the
   flipped half of P4. This is the side of the Rule-4-vs-sense-1 contradiction the team
   ruled for.

2. **Non-qualifying sense 1 — instrument-boundary ruling (excluded half) + F3a.**
   Rewritten from "Technical or measurement reproducibility within a study" to
   "Technical or measurement precision that does not concern an already published
   instrument's properties", scoping it explicitly to device-to-device and
   sample-to-sample precision, inter/intra-rater agreement computed as part of this
   study's own methods, and multi-laboratory round-robin, ring-trial or
   proficiency-testing exercises run as laboratory quality assurance (that clause is
   F3a). The old trailing sentence "It does qualify when the paper aims to replicate
   previously published estimates of those properties" is replaced by a pointer to rule
   4, so the rule is stated once and in one place.
   *Motivating pattern:* P4 (round-robin / inter-rater / technical reproducibility).
   *Contradiction resolved:* sense 1 and rule 4 can no longer both apply — sense 1 now
   covers only work that is **not** about a published instrument's properties, and
   hands everything else to rule 4 by name.

3. **Qualifying rule 6 — F4 self-declaration guard.**
   Appended: author self-declaration applies only when the declared target is earlier
   published research in another paper; a declared "replication" whose target is
   elsewhere in this same paper is an internal replication.
   *Motivating pattern:* P2 (internal replication across studies in one paper) —
   flash-lite and ministral-14b both answer qualifying whenever the word "replication"
   appears, citing rule 6.

4. **Internal-replication bullet — F4 + F1.**
   Generalised from "a paper whose Study 2 replicates its own Study 1" to any re-test of
   a result obtained elsewhere in this same paper, thesis or dissertation, whatever the
   authors call it, with the three observed phrasings named (F4). Then the two-stage
   discovery design is named as an instance, with the cue words "replication set /
   sample / cohort", "stage 2" and "confirmed in an independent cohort", and the guard
   sentence that re-admits any abstract saying the association was reported by earlier
   research (F1).
   *Motivating patterns:* P2 (4 leaks) and P1 (8 leaks). The F1 guard sentence is
   deliberate recall protection for genuine external GWAS replications.

5. **Non-qualifying sense 2 — F2 (tool-benchmark loophole).**
   Extended to assays and apparatus, and stated to hold even when the abstract names the
   published results the tool recovers and uses the words "reproduce", "replicate",
   "verify" or "validate against". The exception is rewritten to protect a study whose
   aim is to settle whether the earlier claim itself holds, **including a re-analysis of
   the earlier study's own data** — the explicit clause that keeps computational
   reproductions qualifying.
   *Motivating pattern:* P3 (8 leaks, ministral-14b wrong on all 8).

6. **Non-qualifying sense 3 — F6 (framework reuse / deployment).**
   Added histological replication, reuse of an earlier paper's framework, element list
   or protocol *as this study's instrument*, and rolling an engineering solution, pilot
   or intervention out to a further site.
   *Motivating pattern:* P7 (vocabulary misfire). The wording "as this study's
   instrument" is what keeps a genuine conceptual replication that reuses the original's
   materials outside this clause.

### Deliberately not applied

- **F5** (make "the aim, not a result" operational, pattern P6) — moderate recall risk,
  and it pushes gpt-5.4-mini, already the strictest model on declared intent, further
  toward "none".
- **F3-full** (rewrite rule 4 so translations and first applications are excluded,
  patterns P4+P5) — it points at exactly the positives the team ruling has now decided
  are in scope, and would contradict change 1.

## Unified diff

```diff
--- prompt_v3.txt	2026-08-01 12:50:37
+++ prompt_v31.txt	2026-08-01 14:47:32
@@ -63,9 +63,9 @@
 1. Context transfer. Re-testing the same claim in a different population, country, language or setting qualifies.
 2. Conceptual replication. Re-testing the claim with a changed method, measure or paradigm qualifies.
 3. Self re-test. Authors re-testing their own earlier published finding in a separate paper qualifies.
-4. Measurement re-validation. Re-testing, re-validating or evaluating the reproducibility of an already published instrument, scale, test or clinical procedure qualifies, including when this is done in a new population, language or setting.
+4. Measurement re-validation. Re-testing, re-validating, translating or adapting an already published instrument, scale, test or clinical procedure qualifies, including when this is done in a new population, language or setting, and including when the stated aim is that instrument's reliability, test-retest, inter-rater agreement or reproducibility in that new population, language or setting.
 5. Comment or reply with its own analysis. A comment, reply or letter that presents its own re-analysis of a published result qualifies.
-6. Author self-declaration. If the authors explicitly identify the study itself as a replication or reproduction, accept that framing and use the type they declare. Merely using related vocabulary in one of the non-qualifying senses below is not a self-declaration.
+6. Author self-declaration. If the authors explicitly identify the study itself as a replication or reproduction, accept that framing and use the type they declare. Merely using related vocabulary in one of the non-qualifying senses below is not a self-declaration. Author self-declaration applies only when the declared target is earlier published research in another paper; a declared "replication" whose target is elsewhere in this same paper is an internal replication.
 
 WHAT DOES NOT QUALIFY
 
@@ -75,13 +75,13 @@
 - Target specificity. The thing being checked must be a particular finding that someone reported, not the accepted background knowledge of a field. A study that tests whether something the literature already widely holds applies in its own sample is ordinary research building on prior work. Example of accepted background knowledge: "the well-established association between X and Y". The abstract does not have to name the source study: a paper qualifies if it clearly aims to check a particular finding reported by earlier research.
 - First validation of a new instrument. The initial validation of a newly proposed instrument does not qualify, because there is no earlier reported finding to check. By contrast, re-validating an already published instrument qualifies.
 - Comment without analysis. A comment or letter that only argues about an earlier study, presenting no new data and no re-analysis, does not qualify.
-- Internal replication. A paper whose Study 2 replicates its own Study 1 does not qualify. The target must be earlier published research in some paper other than this one.
+- Internal replication. A re-test of a result obtained elsewhere in this same paper, thesis or dissertation does not qualify, whatever the authors call it — "Study 6 was a replication of Study 2", "two experiments that are a replication of one another", or a second objective that replicates a pattern the paper itself has just reported. The target must be earlier published research in some paper other than this one. This covers the two-stage discovery design: a study that identifies a signal in its own discovery sample and then confirms it in its own second sample does not qualify, however that second sample is labelled. "Replication set", "replication sample", "replication cohort", "stage 2" and "confirmed in an independent cohort" describe a design internal to the paper, not a check on another paper's finding. It qualifies only if the abstract says the association being confirmed was reported by earlier research.
 
 The words "replication", "reproduce", "reproducibility" and their relatives are used in several senses that do not qualify:
 
-1. Technical or measurement reproducibility within a study — sample-to-sample precision, device-to-device precision, inter-rater agreement, test-retest agreement. It does qualify when the paper aims to replicate previously published estimates of those properties.
-2. Tool benchmarking: a new model, simulation or numerical method demonstrated to reproduce known results in order to show that the tool works. Exception: a study that uses such a tool to check a published result qualifies.
-3. Ordinary-language, biological and field-specific senses: DNA, viral or cell replication; "replication" as a count of overlapping samples in a chronology; "replicated across sites" describing the internal design of the study being reported.
+1. Technical or measurement precision that does not concern an already published instrument's properties — sample-to-sample precision, device-to-device precision, inter-rater or intra-rater agreement computed as part of this study's own methods, and multi-laboratory round-robin, ring-trial or proficiency-testing exercises run as laboratory quality assurance. The aim of these studies is to quantify the spread of a measurement, not to check a property earlier research reported. Reliability, test-retest, inter-rater or reproducibility work on an already published instrument, scale, test or procedure is rule 4 above and qualifies.
+2. Tool benchmarking: a new model, simulation, numerical method, assay or apparatus demonstrated to reproduce known results in order to show that the tool works. This holds even when the abstract names the published results the tool recovers, and even when it uses the words "reproduce", "replicate", "verify" or "validate against" — recovering what earlier work reported is a property of the tool, and the tool is the paper's subject. Exception: a study whose aim is to settle whether the earlier claim itself holds, including a re-analysis of the earlier study's own data, qualifies.
+3. Ordinary-language, biological and field-specific senses: DNA, viral, cell or histological replication; "replication" as a count of overlapping samples in a chronology; "replicated across sites" describing the internal design of the study being reported; reuse of an earlier paper's framework, element list or protocol as this study's instrument ("using 16 elements of X's research"); and rolling an engineering solution, pilot or intervention out to a further site.
 4. Papers about replication: reviews, commentary on the replication crisis, or a paper that merely states that future replication is needed.
 
 CONFIDENCE
```
