# Extraction Failure Audit Report

Generated: 2026-06-10 12:24:41

## Current Extraction State

- Total extracted rows: 1378
- Missing DOI (doi_o empty/pending): 25
- API error count: 0
- Target pending count: 0

## Link Method Distribution

| link_method       |   count |   percentage |
|:------------------|--------:|-------------:|
| author_year_match |    1217 |         88.3 |
| llm_fulltext      |     160 |         11.6 |
| no_original_found |       1 |          0.1 |

## Link Confidence Distribution

| link_confidence   |   count |   percentage |
|:------------------|--------:|-------------:|
| high              |    1267 |         91.9 |
| medium            |      74 |          5.4 |
| low               |      37 |          2.7 |

## Link Method vs. Confidence

| link_method       |   high |   low |   medium |   All |
|:------------------|-------:|------:|---------:|------:|
| author_year_match |   1112 |    34 |       71 |  1217 |
| llm_fulltext      |    154 |     3 |        3 |   160 |
| no_original_found |      1 |     0 |        0 |     1 |
| All               |   1267 |    37 |       74 |  1378 |

## Rows with Missing DOI (Sample)

Total: 25 rows

| doi_r                        | title_r                                                                                                                            | authors_r                                                              |   year_r | link_method       | link_confidence   |
|:-----------------------------|:-----------------------------------------------------------------------------------------------------------------------------------|:-----------------------------------------------------------------------|---------:|:------------------|:------------------|
| 10.23668/psycharchives.2392  | Quantifying Replication Value: A formula-based approach to study selection in replication research.                                | Peder Mortvedt Isager                                                  |     2019 | no_original_found | high              |
| 10.34917/4332616             | Principal Change Facilitator Style and Student Achievement: A Study of Schools in the Middle                                       | Steven Keith Stewart                                                   |     2020 | author_year_match | high              |
| 10.1016/j.intell.2004.12.001 | Constructive replication of the visual–perceptual-image rotation model in Thurstone's (1941) battery of 60 tests of mental ability | William Johnson; T BOUCHARDJR                                          |     2005 | author_year_match | high              |
| 10.5772/50172                | Role of Clay Minerals in Chemical Evolution and the Origins of Life                                                                | Hideo Hashizume                                                        |     2012 | author_year_match | high              |
| 10.1037//0022-006x.55.1.115  | Replication and extension of the Teacher Self-Control Rating Scale.                                                                | William Work; A. Dirk Hightower; John W. Fantuzzo; Cynthia A. Rohrbeck |     1987 | author_year_match | medium            |
| 10.5964/ejop.v15i3.2103      | Mentalities and mind-sets: The skeleton of relative stability in psychology’s closet                                               | Gordon Sammut                                                          |     2019 | author_year_match | high              |
| 10.1145/2168556.2168644      | Revisiting Russo and Leclerc                                                                                                       | Poja Shams; Erik Wästlund; Lars Witell                                 |     2012 | author_year_match | high              |
| 10.58379/lzzz5040            | Interaction in a paired oral assessment: Revisiting the effect of proficiency                                                      | Young‐A Son                                                            |     2016 | author_year_match | medium            |
| 10.17705/1atrr.00051         | Emotional Dissonance and the IT Professional – A Replication                                                                       | Laurie Giddens; Cynthia K. Riemenschneider                             |     2020 | author_year_match | high              |
| 10.17705/1atrr.00062         | Emotional Dissonance and the Information Technology Professional: A Methodological Replication Study                               | Sam Zaza; Michael A. Erskine; Stoney Brooks; Sidney A. Morris          |     2020 | author_year_match | high              |

... and 15 more rows
