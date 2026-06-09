# Extraction Failure Audit Report

Generated: 2026-06-06 11:06:12

## Current Extraction State

- Total extracted rows: 1027
- Missing DOI (doi_o empty/pending): 17
- API error count: 0
- Target pending count: 0

## Link Method Distribution

| link_method       |   count |   percentage |
|:------------------|--------:|-------------:|
| author_year_match |     866 |         84.3 |
| llm_fulltext      |     160 |         15.6 |
| no_original_found |       1 |          0.1 |

## Link Confidence Distribution

| link_confidence   |   count |   percentage |
|:------------------|--------:|-------------:|
| high              |     952 |         92.7 |
| medium            |      49 |          4.8 |
| low               |      26 |          2.5 |

## Link Method vs. Confidence

| link_method       |   high |   low |   medium |   All |
|:------------------|-------:|------:|---------:|------:|
| author_year_match |    797 |    23 |       46 |   866 |
| llm_fulltext      |    154 |     3 |        3 |   160 |
| no_original_found |      1 |     0 |        0 |     1 |
| All               |    952 |    26 |       49 |  1027 |

## Rows with Missing DOI (Sample)

Total: 17 rows

| doi_r                       | title_r                                                                                                                                  | authors_r                                                     |   year_r | link_method       | link_confidence   |
|:----------------------------|:-----------------------------------------------------------------------------------------------------------------------------------------|:--------------------------------------------------------------|---------:|:------------------|:------------------|
| 10.17705/1atrr.00051        | Emotional Dissonance and the IT Professional – A Replication                                                                             | Laurie Giddens; Cynthia K. Riemenschneider                    |     2020 | author_year_match | high              |
| 10.17705/1atrr.00062        | Emotional Dissonance and the Information Technology Professional: A Methodological Replication Study                                     | Sam Zaza; Michael A. Erskine; Stoney Brooks; Sidney A. Morris |     2020 | author_year_match | high              |
| 10.23668/psycharchives.2392 | Quantifying Replication Value: A formula-based approach to study selection in replication research.                                      | Peder Mortvedt Isager                                         |     2019 | no_original_found | high              |
| 10.1108/ccm-11-2013-0180    | A context-specific model of organizational trust                                                                                         | Carvell N. McLeary; Paula A. Cruise                           |     2015 | author_year_match | high              |
| 10.1111/cogs.12849          | Syntactic Creativity Errors in Children's Wh‐Questions                                                                                   | C. Jane Lutken; Géraldine Légendre; Akira Omaki               |     2020 | author_year_match | high              |
| 10.34917/4332616            | Principal Change Facilitator Style and Student Achievement: A Study of Schools in the Middle                                             | Steven Keith Stewart                                          |     2020 | author_year_match | high              |
| 10.31234/osf.io/4qnfk       | Goodness of the side of the dominant hand: A registered direct replication of Casasanto (2009)                                           | Kyoshiro Sasaki; Fumiya Yonemitsu; Yuki Yamada                |     2019 | author_year_match | high              |
| 10.1080/13504860903075621   | Static Replication of Forward-Start Claims and Realized Variance Swaps                                                                   | Jan Baldeaux; Marek Rutkowski                                 |     2010 | author_year_match | high              |
| 10.1017/s0958344023000034   | Student satisfaction and perceived learning in an online second language learning environment: A replication of Gray and DiLoreto (2016) | Hye Won Shin; Sarah Sok                                       |     2023 | author_year_match | high              |
| 10.1109/thms.2018.2806200   | Visual Sampling Processes Revisited: Replicating and Extending Senders (1983) Using Modern Eye-Tracking Equipment                        | Yke Bauke Eisma; Christopher Cabrall; Joost de Winter         |     2018 | author_year_match | high              |

... and 7 more rows
