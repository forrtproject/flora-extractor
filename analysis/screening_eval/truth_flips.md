# Truth flips under the v3.1 instrument-boundary ruling

The team ruling applied here:

> Re-testing, re-validating, translating or adapting an **already published**
> instrument, scale, test or procedure **qualifies** — including when the stated aim is
> reliability, test-retest, inter-rater agreement or reproducibility of that instrument
> in a new population, language or setting.
>
> Still excluded: the **first** validation of a **newly proposed** instrument; and
> technical precision / agreement work that does not concern a published instrument's
> properties (device-to-device precision, inter-rater agreement done as part of a
> study's own methods, round-robin / ring-trial / proficiency testing run as laboratory
> quality assurance rather than as a check of previously reported properties).

**Scope.** Every hard true negative flagged P4 or P5 in `leak_analysis.md`, plus the 11
arguable-label cases from its §2 table. That is 17 distinct ids: P4 (HU02, HU09, HU18,
HU40, HU46, H001, H008), P5 (HU17, HU58, H003, H006, H014, H026), and the arguable
cases outside those two patterns (HU07, HU12, HU28, HU51). Title and abstract were read
for all 17. No label outside this set was touched.

**Result: 7 flips to `yes`, 10 unchanged.**

New truth files: `human_truth_v31.json` (60 ids: 47 `no`, 13 `yes` — was 50/10) and
`heldout_truth_v31.json` (30 ids: 20 `no`, 10 `yes` — was 24/6). Same shape as their
sources; only the flipped ids differ.

---

## Flips to `yes` (7)

| id | pattern | title | why it flips |
| --- | --- | --- | --- |
| `HU17` | P5 | Research of the possibility of using *Ceriodaphnia affinis* Lilljeborg (Crustacea) in a short-term test while setting ecological quality standards in Ukraine | OECD No. 202 is an already published, standardised test procedure; the paper's stated purpose is to test that procedure with a new test organism and establish its metrological characteristics there — adaptation of a published test to a new setting. |
| `HU46` | P4 | Using Long-Term Outdoor Exposure Data to Benchmark Accelerated Durability Test Methods | The targets are published ASTM and CSA standard test methods and their published pass/fail limits; the aim is to verify the appropriateness of those methods and limits against long-term field data — a check of previously reported properties of a published procedure, not lab QA. |
| `HU58` | P5 | Non-Invasive and Invasive Assessment of Carbohydrate Intakes and Muscle Glycogen Utilisation in Rugby League and AFL | MuscleSound is an already published/commercial ultrasound system validated in prior cycling studies; the stated aim is its reliability (test-retest reproducibility) and validity in a new population of team-sport athletes. Exactly the reliability-in-a-new-population case the ruling admits. |
| `H003` | P5 | Psychometric Properties of the Persian Version of Dizziness Handicap Inventory | Translation of the published Dizziness Handicap Inventory into Persian, with internal consistency, test-retest reproducibility and factor structure assessed against the original DHI's structure. Translation + reliability of a published instrument. |
| `H006` | P5 | Adaptación psicométrica preliminar del Child Abuse Potential Inventory en Venezuela | Cultural adaptation and validation of the published CAPI in a Venezuelan population, with the recovered factor structure compared to the original scale's. Adaptation of a published instrument to a new population and language. |
| `H014` | P5 | Cattle gastrointestinal nematode egg-spiked faecal samples: high recovery rates using the Mini-FLOTAC technique | Mini-FLOTAC is a published, recommended technique; the abstract names the single prior paper that evaluated its recovery rate and sets out to "further study" that rate — sensitivity, accuracy, precision and reproducibility — in two new laboratories. A check of a published procedure's previously reported performance. |
| `H026` | P5 | Applicability of the Greulich–Pyle Method in Assessing the Skeletal Maturity of Children in the Eastern Uttar Pradesh (UP) Region | The GP atlas is a long-published method whose applicability "has been recapitulated in many studies"; the stated aim is to assess its applicability and reproducibility in a new regional population. Re-validation of a published test in a new population. |

## Not flipped (10)

| id | pattern | title | why it stays `no` |
| --- | --- | --- | --- |
| `HU02` | P4 | Quantum Hall Resistance Standard in Graphene Grown by CVD on SiC | The stated objective is sample-to-sample reproducibility of the electronic properties and performance of the authors' own devices — device-to-device precision, no published instrument's reported property is the target. Explicitly excluded. |
| `HU09` | P4 | Understanding of Pharmacokinetics and Exposure Drives use of Nintedanib as a Positive Control Reference Item… | "Anti-fibrotic reproducibility" means consistent efficacy of a control dose across the authors' own four studies — internal consistency of this project's model, part of the study's own methods, not a published instrument's properties. |
| `HU18` | P4 | An Inter-Laboratory Study of Zn–Sn–Ti–O Thin Films using High-Throughput Experimental Methods | A NIST/NREL inter-laboratory comparison of high-throughput workflows, framed as a feasibility case study for a materials collaboratory. Round-robin measurement-spread work; no published instrument property is being re-checked. |
| `HU40` | P4 | Uniform Noting for International Application of the Tumor-Stroma Ratio… | The TSR is a method these authors state they developed; part 1 measures inter-pathologist reliability inside the study's own quality-assessment programme and part 2 is the first prospective validation. First validation of a newly proposed instrument — the retained exclusion. |
| `H001` | P4 | Application of a Sanger-Based External Quality Assurance Strategy for the Transition of HIV-1 Drug Resistance Assays to NGS | Proficiency-testing panels distributed to ten laboratories to evaluate *internally developed* NGS assays — laboratory quality assurance, and the assays are new. Both exclusions apply. |
| `H008` | P4 | A round robin test on the close-proximity method: comparison of four CPX trailers | A round robin across four trailers, i.e. device-to-device precision, and the abstract states the reproducibility of the CPX method "is still not well known" — there is no previously reported property being checked, the study establishes one. Excluded round-robin QA. (Closest call among the non-flips: HU46 flips because published standard *limits* are the declared target, H008 does not because no such prior figure exists.) |
| `HU07` | P3 (arguable) | Impedance Control of a Transfemoral Prosthesis… | Outside the ruling's scope. A least-squares estimation method is extended so that it recovers published perturbation-study values, in order to show the method works — tool benchmarking (non-qualifying sense 2), which the ruling does not touch. |
| `HU28` | P3 (arguable) | On Carr and Lee's Correlation Immunization Strategy | Outside the ruling's scope. Monte Carlo experiments evaluate a published pricing/hedging strategy's effectiveness; this is a method-evaluation/tool-benchmark case, not the re-validation of a measurement instrument in a new population or language. |
| `HU12` | P6 (arguable) | Cognitive vulnerability to sleep deprivation is robustly associated with two dynamic connectivity states | Outside the ruling's scope. The arguable point is self re-test versus incidental agreement (pattern P6), not the instrument boundary; the ruling gives no basis to move it. |
| `HU51` | P1 (arguable) | Efficient identification of trait-associated loss-of-function variants in the UK Biobank cohort… | Outside the ruling's scope. The arguable point is the internal discovery/replication design (pattern P1), decided by the internal-replication rule, not by the instrument boundary. |

## Notes

- Four of the ten non-flips (HU07, HU28, HU12, HU51) are arguable cases whose
  arguability comes from patterns P1, P3 and P6. The ruling settles only the P4/P5
  instrument boundary, so they are left as they were rather than decided on an
  unrelated basis.
- The remaining six non-flips are all P4 and all land in the retained exclusions:
  device-to-device precision (HU02, H008), the study's own methods (HU09, HU18),
  first validation of a new instrument (HU40), proficiency-testing QA (H001).
- After the flips, all six P5 cases qualify and one of seven P4 cases does. That is the
  shape the ruling intends: translations and adaptations in, laboratory precision work
  out.
