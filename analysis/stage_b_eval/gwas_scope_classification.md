# Are the genetics gold positives that the GWAS guard / BIOLOGICAL_OF kill in scope?

Judged strictly against `shared/prompts.py` (`_CLASSIFY_PROMPT`, WHAT DOES NOT QUALIFY →
Internal replication, lines ~312–330): a paper qualifies only when the association it
re-tests is attributed to **earlier published research in another paper**. A two-stage
discovery + replication-cohort design internal to the paper does not qualify "however that
second sample is labelled".

Per-paper table: `gwas_scope_classification.csv`
(doi, title, subset, classification, evidence_quote, recovered_by_fix).

## 1. The affected sets

Reconstructed by running production `filter.phrase_detection.keyword_verdict` over the
gold-positive corpus (`.claude/worktrees/issue-130-prescreen/analysis/prescreen_eval/override_positives.json`,
n = 7,505).

| set | n |
| --- | ---: |
| Gold positives killed by the `_GWAS_GUARD` phrase guard (every matching replication phrase guarded out, no exclusion pattern firing) | **33** (30 `negative`, 3 `ambiguous` via title stem) |
| Gold positives killed by the `BIOLOGICAL_OF` exclusion pattern | **36** |
| … of which the text is genetics/genomics, not virology/molecular | **26** |
| … of which the text is virology / cell biology / molecular | **10** |
| **Union (GWAS guard ∪ BIOLOGICAL_OF-genetics)** | **59** (no overlap) |

(The vocabulary report's "30" for the guard row counts only the `negative` outcomes; the
three title-stem `ambiguous` rows are still kept out of the positive tier by the guard.)

## 2. Composition against the rule

| subset | SENSE_3 qualifies | SENSE_2 internal | SENSE_1 molecular | AMBIGUOUS |
| --- | ---: | ---: | ---: | ---: |
| GWAS phrase guard (33) | 28 | 3 | 0 | 2 |
| BIOLOGICAL_OF, genetics (26) | 19 | 3 | 0 | 4 |
| **Union (59)** | **47 (80%)** | **6 (10%)** | 0 | **6 (10%)** |
| BIOLOGICAL_OF, non-genetics (10) — context only, not in the union | 0 | 0 | 10 | 0 |

The dominant pattern in both subsets is the same and it is SENSE 3: a set of loci that an
earlier published GWAS reported is genotyped in a new population or cohort and the prior
association is re-tested. Examples: "Replication of the Wellcome Trust genome-wide
association study on essential hypertension in a Korean population"; "we replicated eight
loci associated with lipid levels previously reported in a European population"; "Recently,
the Type 1 Diabetes Genetics Consortium (T1DGC) reported 22 novel loci … this study aims to
replicate the association in three independent GWAS cohorts". These are ordinary
context-transfer replications under qualifying rule 1, and the guard/exclusion is a
false negative on them, not a gold-corpus error.

Two SENSE_3 papers qualify on the attribution test but arguably fail another clause of the
rule, and are flagged in the CSV quote:

- `10.1002/gepi.22167` — "To test the merged imputed genotype set, we replicated a
  previously reported chromosome 6 HLA-B herpes zoster association" — tool/dataset
  benchmarking (non-qualifying sense 2).
- `10.1002/cpt.2337` — "To validate the drug response phenotype, we replicated the
  previously established association …" — the re-test is instrumental, not the paper's aim.

## 3. Value of the two proposed fixes, re-measured

Both fixes were applied exactly as specified and re-run over the killed sets.

### G1 — exempt the GWAS guard when the sentence names a prior report
Candidate regex from `vocab_candidates.csv`:
`(?i)\b(?:previously|prior|earlier|original|published|reported\s+by|replicated\s+the\s+association)\b`
applied to the guarded sentence.

| | n | SENSE_3 | SENSE_2 internal | AMBIGUOUS |
| --- | ---: | ---: | ---: | ---: |
| recovered by G1 as written | 19 | 17 | **2** | 0 |
| not recovered | 14 | 11 | 1 | 2 |

G1 as written recovers 17 of the 28 genuine SENSE_3 papers (61%) and drags in 2 of the 3
internal-design papers. Both leaks come from one alternative in the regex —
`replicated\s+the\s+association`, which is not a prior-report cue at all:

- `10.1002/hbm.22247`: "We replicated the association of this single-nucleotide
  polymorphism with regional tissue volumes in a large sample of young participants" —
  the SNP was discovered in the paper's own ADNI sample two sentences earlier.
- `10.1038/s41598-023-31701-w`: "In addition, we replicated the association between
  rs1564939 in the GLRA3 gene and DKD" — the signal came from the paper's own GWAS.

**Dropping that one alternative** (keeping `previously|prior|earlier|original|published|
reported by`) recovers **12 papers, 12/12 SENSE_3, 0 internal**: cpt.2337, gepi.22167,
oby.2010.256, s41380-020-0672-1, hmg/ddu392, msystems.00502-20, epi-15-1217, s12952-015-0029-5,
s40478-021-01250-2, pgen.1009564, pone.0082420, ppat.1008818. So the choice is precision
(12 recovered, 100% in scope) vs. recall (19 recovered, 89% in scope). Either way the guard
keeps killing ~11–16 genuine SENSE_3 papers, because most of them put the prior-report
attribution in a *different sentence* from the "we replicated …" sentence (e.g. srep08194:
attribution in sentence 1, replication claim in sentence 2) — a same-sentence cue cannot
see it.

### BO1 — BIOLOGICAL_OF: `genome(s)`/`genomic` no longer match inside `genome-wide`
Patch from `exclusion_narrowing_report.md` §5a, applied to the live pattern.

| | n | SENSE_3 | SENSE_2 internal | SENSE_1 molecular | AMBIGUOUS |
| --- | ---: | ---: | ---: | ---: | ---: |
| recovered by BO1 (of all 36 BIOLOGICAL_OF-killed) | 20 | 17 | 1 | **0** | 2 |
| still killed | 16 | 2 | 2 | 10 | 2 |

BO1 is the cleaner of the two fixes: it recovers no virology paper at all, 17 of the 19
genetics SENSE_3 papers (89%), one internal-design paper (`10.1371/journal.pone.0174642`,
a Korean TB GWAS with its own discovery + validation set) and two AMBIGUOUS ones (an
editorial, a biobank-wide replication-rate survey). The two genetics SENSE_3 papers it
misses are killed by a different token in the same pattern — `chromosome`
(`10.1186/2040-2392-5-13`, "Replication of linkage at chromosome 20p13") and a
non-`genome` span (`10.1002/pros.21320`).

**Answer to the question posed:** yes, both fixes preferentially recover papers that
genuinely qualify. BO1 recovers 85% in-scope and 0% molecular; G1 as written recovers 89%
in-scope, and 100% if its `replicated the association` alternative is dropped. Neither fix
opens a door to the two-stage discovery design as a class — the internal-design papers that
slip through do so on a wording that would fool any lexical rule, and are a handful.

## 4. Gold-corpus errors (FLoRA positives the project's own rule excludes)

### 4a. SENSE_2_INTERNAL — 6 papers, all in the union

| doi | title | quote that settles it |
| --- | --- | --- |
| 10.1002/hbm.22247 | A commonly carried genetic variant in OPRD1 … Replication in elderly and young populations | "we first examined a large sample of 738 elderly participants … One very common variant (rs678849) predicted differences in regional brain volumes. We replicated the association of this single-nucleotide polymorphism … in a large sample of young participants" |
| 10.1038/s41380-021-01176-0 | A metabolome-wide association study … serum laurylcarnitine … depression | "1411 participants of the KORA F4 study (discovery cohort) … We replicated our results in an independent sample of 968 participants of the SHIP-Trend study … (replication cohort)" |
| 10.1038/s41598-023-31701-w | A GWAS identifies a possible role for cannabinoid signalling in diabetic kidney disease | "GWAS suggestive results (P < 1 × 10⁻⁵) were further replicated using summary statistics from three cohorts" |
| 10.1161/circgenetics.110.959205 | Genome-Wide Significance and Replication of the Chromosome 12p11.22 Locus … Peripartum Cardiomyopathy | "This study evaluated and replicated genome-wide association of single nucleotide polymorphisms with PPCM … A replication study of independent population samples used new cases (PPCM2, n=30)" |
| 10.1371/journal.pone.0174642 | Risk prediction of pulmonary tuberculosis … adult Korean population | "We conducted a GWAS using 467 PTB cases and 1,313 healthy controls … and validated the results in an independent Korean population" |
| 10.6084/m9.figshare.c.3629678_d4 | Additional file 1: of Discovery and replication of a peripheral tissue DNA methylation biosignature … [Data set] | title design is "Discovery and replication"; the record itself is a figshare data deposit ("Table containing all probes nominally associated with AUC weekday cortisol"), not a study |

### 4b. SENSE_1_MOLECULAR — 10 papers (BIOLOGICAL_OF, non-genetics; correctly killed)

| doi | title | quote |
| --- | --- | --- |
| 10.1016/j.virol.2017.11.022 | Coronaviruses and arteriviruses display striking differences in their cyclophilin A-dependence during replication in cell culture | "replication of several nidoviruses was reported to depend on …" |
| 10.1038/s41467-018-03981-8 | Variation in Wolbachia effects on Aedes mosquitoes … | "replication of dengue viruses in the …" |
| 10.1038/s41598-020-80577-7 | Targeted disruption of pi–pi stacking in Malaysian banana lectin … | "replication of HIV" |
| 10.1055/s-0036-1597524 | SEC14L2 is not a reliable predictor of HCV replication fitness | "replication of natural patient derived HCV" |
| 10.1117/12.876393 | Growth and replication of red rain cells at 121 °C … | "replication of red rain cells at 121 °C" |
| 10.1128/jvi.00486-10 | The Proteasome Inhibitor Velcade Enhances rather than Reduces Disease in Mouse Hepatitis Coronavirus-Infected Mice | "replication of the virus in mice" |
| 10.1371/journal.pone.0146229 | Piscine Orthoreovirus from Western North America … | "replication of viral RNA" |
| 10.3389/fcimb.2018.00109 | Opposite Effects of Two Human ATG10 Isoforms on Replication of a HCV Sub-genomic Replicon … | "Replication of a HCV Sub-genomic Replicon" |
| 10.4161/chim.1.2.13891 | Why are levels of maternal microchimerism higher in type 1 diabetes pancreas? | "replication of these cells" |
| 10.46582/jsrm.0603008 | 293FT cells transduced with four transcription factors … | "replication of transfected plasmids containing the" |

Together, 16 of the 69 papers examined (6 internal + 10 molecular) are gold-corpus errors:
FLoRA lists them as positives, the project's own rule excludes them. They should be
flagged for correction rather than counted against the filter's recall.

### 4c. AMBIGUOUS — 6 papers, not counted either way

`10.1038/s41598-018-35871-w` and its preprint `10.1101/166710` (55,000 replicated mQTL —
the abstract never says whose mQTL are re-tested); `10.1038/s41398-018-0234-3` (own module
checked against a previous RNA-Seq study); `10.1097/hjh.0b013e328341c6c9` (editorial on
Ho et al.); `10.1186/1471-2105-12-s11-a5` (conference abstract, background only);
`10.3390/genes15070931` (UKB/FinnGen survey of replication rates as a phenomenon).
