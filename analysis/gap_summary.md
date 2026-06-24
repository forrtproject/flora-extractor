# Task 2: Overlap Analysis Report

Generated: 2026-06-23 20:40:01

## Executive Summary

- **Gap Analysis Results** (known replications absent from candidates.csv):
  - Gaps with DOI: 1447
  - Gaps URL-only (no DOI): 409
  - Fuzzy-matched (found via title, not a gap): 0
  - Total genuine gaps: 1856
- **Filter misclassifications**: 0 rows

## Recall Gaps (Analysis 1a)

Known replications in all_replications.csv absent from candidates.csv: 1856

### Gaps with DOI (1447 rows)
|    | doi_r                               | study_r                                                                                                                           |   year_r |
|---:|:------------------------------------|:----------------------------------------------------------------------------------------------------------------------------------|---------:|
|  0 | 10.1001/archpediatrics.2010.16      | Cost-effectiveness of a Motivational Intervention to Reduce Rapid Repeated Childbearing in High-Risk Adolescent Mothers           |     2010 |
|  1 | 10.1001/jama.2010.1692              | How to Use an Article About Quality Improvement                                                                                   |     2010 |
|  2 | 10.1002/9780470015902.a0001055.pub2 | Eukaryotic Replication Origins and Initiation of <scp>DNA</scp> Replication                                                       |     2010 |
|  3 | 10.1002/9780470061602.eqf04008      | Second Fundamental Theorem of Asset Pricing                                                                                       |     2010 |
|  4 | 10.1002/9780470688618.taw0213       | The Replication of Viruses                                                                                                        |     2010 |
|  5 | 10.1002/9780470688618.taw0237       | Poxvirus Replication                                                                                                              |     2010 |
|  6 | 10.1002/9781444327632               | Handbook of Developmental Science, Behavior, and Genetics                                                                         |     2010 |
|  7 | 10.1002/adfm.200901700              | Nanopatterning by an Integrated Process Combining Capillary Force Lithography and Microcontact Printing                           |     2010 |
|  8 | 10.1002/adma.200903655              | Nanoporous Metal Membranes with Bicontinuous Morphology from Recyclable Block‐Copolymer Templates                                 |     2010 |
|  9 | 10.1002/ajmg.b.31098                | Association analysis of <i>PALB2</i> and <i>BRCA2</i> in bipolar disorder and schizophrenia in a scandinavian case–control sample |     2010 |


### Gaps URL-only / no DOI (409 rows)
|    | url_r                            | study_r                                                                                                                             |   year_r |
|---:|:---------------------------------|:------------------------------------------------------------------------------------------------------------------------------------|---------:|
|  0 | https://openalex.org/W7110572191 | Reexamining the effect of gustatory disgust on moral judgment: A multi-lab direct replication of Eskine, Kacinik, and Prinz (2011)  |     2023 |
|  1 | https://openalex.org/W7110695867 | Replication of T Meiser, C Sattler, K Weisser (2008, JEPLMC 34(1), Exp. 3)                                                          |     2023 |
|  2 | https://openalex.org/W7110696601 | Cognitive Biases and Religious Belief: A path model replication in the Czech Republic and Slovakia with a focus on anthropomorphism |     2023 |
|  3 | https://openalex.org/W7110695867 | Replication of T Meiser, C Sattler, K Weisser (2008, JEPLMC 34(1), Exp. 3)                                                          |     2023 |
|  4 | https://openalex.org/W7117210261 | 1 Student Project: Replication of Klein, Thielmann, Hilbig &amp; Zettler (2017, JDM, Study 1)                                       |     2023 |
|  5 | https://openalex.org/W7110536390 | 1 Student Project: Replication of Wang, Geng, Qin &amp; Yao (2016, JDM, Study 2)                                                    |     2023 |
|  6 | https://openalex.org/W7110561066 | 1 Student Project: Replication of Cheek, Coe-Odess &amp; Schwartz (2015, JDM, Study 2b)+                                            |     2023 |
|  7 | https://openalex.org/W7110476225 | Replication of SJ Heine, EE Buchtel, A Norenzayan (2008, PS 19(4), exp 1)                                                           |     2023 |
|  8 | https://openalex.org/W7110641258 | 1 Student Project: Replication of Krijnen, Zeelenberg, &amp; Breugelmans (2015, JDM, Study 6)                                       |     2023 |
|  9 | https://openalex.org/W7110605290 | 1 Student Project: Replication of Mata &amp; Almeida (2014, JDM, Study 3)                                                           |     2023 |


## Filter Gaps (Analysis 1b)

Replications discovered but wrongly filtered: 0

## Recommendations

1. **Investigate recall gaps**: Why are known replications missing? Check search keywords and sources.

2. **Review misclassifications**: Correct filter rules to avoid losing valid replications.

3. **Augment underrepresented sources**: Adjust search strategy for weak sources.

4. **Older pipeline archaeology**: Manually document differences in search strategy.
