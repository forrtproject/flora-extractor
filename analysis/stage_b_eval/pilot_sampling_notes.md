# Stage-B pilot sampling frames — notes

Generated from `pilot_frames.py` (seed 20260803, reservoir sample n≤1000 per arm).

## Scan

- Manifest: `cache/snapshot/manifest.json`, 2,446 partitions, 510,372,821 records (snapshot 2026-06-26).
- Partitions scanned: 40 (failed: 0); manifest indices [235, 286, 337, 388, 466, 517, 568, 619, 673, 724, 775, 826, 880, 937, 988, 1045, 1102, 1153, 1204, 1255, 1309, 1360, 1414, 1465, 1522, 1597, 1663, 1714, 1765, 1819, 1870, 1936, 1993, 2053, 2113, 2164, 2221, 2287, 2338, 2389] — all ≡ 1 mod 3, ≥100k records each, evenly spaced across the eligible list so `updated_date` partitions span the manifest.
- Rows scanned: 10,943,785 (2.1% of the corpus).
- Stage A survivors: 113,554 (1.0%).
- Stage B admitted: 29,149 (0.3% of rows scanned).
- Gate code is imported from the repo, not reimplemented: `_gate_masks`, `_concept_mask`, `_admit` from `search/snapshot_scan.py` and `keyword_verdict` from `filter/phrase_detection.py`; abstracts via `_reconstruct_abstract`. Partitions were streamed over HTTPS range reads with column projection (pyarrow + fsspec) — nothing written to `pilot_partitions/`, so no clash with concurrent agents.

## Arm populations observed

| Arm | n observed | % of admitted | projected share of ~1M full-corpus admissions |
| --- | ---: | ---: | ---: |
| A — positive verdict | 8,027 | 27.5% | ~275,378 |
| B — ambiguous verdict | 16,408 | 56.3% | ~562,901 |
| C — concept-only rescue (verdict negative) | 4,714 | 16.2% | ~161,721 |

Scaling the observed admission rate to the full corpus gives ~1,359,389 admissions overall.

## Sampling method

- Reservoir sampling (Vitter R), one reservoir per arm, capacity 1000, single `random.Random(20260803)` shared across arms and consumed in scan order. Every admitted row in the scanned population had an equal probability of ending in its arm's sample.
- Files written (utf-8-sig): `pilot_sample_positive.csv`, `pilot_sample_ambiguous.csv`, `pilot_sample_concept.csv`. Columns: `openalex_id, doi, title, abstract, year, type, language, verdict, is_reproduction, phrase, reason, exclusion, hit_title_stem, hit_abstract_stem, hit_concept, partition`.
- `hit_title_stem` / `hit_abstract_stem` / `hit_concept` are the Stage A signals that fired; `phrase` is the precise phrase `keyword_verdict` matched (empty for ambiguous title-stem rows); `reason` is its evidence string.

## Descriptives (FULL observed arm populations, not the samples)

| Metric | A — positive verdict | B — ambiguous verdict | C — concept-only rescue (verdict negative) |
| --- | ---: | ---: | ---: |
| n | 8,027 | 16,408 | 4,714 |
| % with abstract | 89.5% | 68.2% | 71.8% |
| % with DOI | 84.0% | 74.5% | 86.4% |
| % title stem hit | 24.5% | 99.7% | 14.5% |
| % abstract stem hit | 86.9% | 45.8% | 48.0% |
| % concept hit | 13.7% | 18.6% | 100.0% |
| % flagged reproduction | 49.9% | 0.0% | 0.0% |
| year missing | 44 | 158 | 16 |

### Year deciles (D0 = min … D10 = max)

| Arm | D0 | D1 | D2 | D3 | D4 | D5 | D6 | D7 | D8 | D9 | D10 |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| A — positive verdict | 1753 | 2005 | 2013 | 2016 | 2019 | 2021 | 2022 | 2023 | 2024 | 2025 | 2026 |
| B — ambiguous verdict | 1649 | 1994 | 2005 | 2011 | 2015 | 2017 | 2019 | 2021 | 2023 | 2025 | 2027 |
| C — concept-only rescue (verdict negative) | 1753 | 2000 | 2009 | 2013 | 2016 | 2017 | 2019 | 2020 | 2023 | 2025 | 2027 |

### Top OpenAlex `type` values

- **A — positive verdict**: article 49.4%, dataset 21.2%, other 15.0%, preprint 6.6%, dissertation 2.3%, review 1.6%, peer-review 1.1%, book-chapter 1.0%
- **B — ambiguous verdict**: article 58.6%, dataset 15.2%, other 11.6%, book-chapter 3.7%, dissertation 3.4%, review 2.6%, preprint 2.3%, book 0.9%
- **C — concept-only rescue (verdict negative)**: article 49.0%, dataset 32.7%, other 9.9%, preprint 2.5%, book-chapter 1.9%, review 1.2%, dissertation 1.0%, peer-review 0.6%

### Language split

- **A — positive verdict**: en 92.9%, (none) 5.4%, pt 0.3%, lv 0.2%, ko 0.2%, es 0.1%, de 0.1%, id 0.1%
- **B — ambiguous verdict**: en 86.0%, es 4.4%, (none) 4.0%, fr 2.2%, pt 1.4%, de 0.5%, ca 0.3%, nl 0.2%
- **C — concept-only rescue (verdict negative)**: en 88.2%, (none) 4.2%, de 1.3%, fr 1.2%, sv 0.5%, pt 0.5%, zh 0.4%, es 0.4%

## Gold-corpus preview

Known-FLoRA DOIs (`doi_r` from `data/all_replications.csv`, `data/flora.csv`, `data/flora_entry_sheet.csv`, plus `analysis/prescreen_eval/cases_live_goldpos_flora.json`): 26,983 distinct DOIs.

| Arm | gold hits | % of arm rows | % of arm rows WITH a DOI | share of all gold hits |
| --- | ---: | ---: | ---: | ---: |
| A — positive verdict | 408 | 5.1% | 6.1% | 89.7% |
| B — ambiguous verdict | 15 | 0.1% | 0.1% | 3.3% |
| C — concept-only rescue (verdict negative) | 32 | 0.7% | 0.8% | 7.0% |

455 gold papers fell inside the scanned population overall. The scan covers 2.1% of the corpus and each work sits in exactly one `updated_date` partition, so ~579 gold papers were scannable in principle — the gate found most of them, and ~89.7% of the ones it found landed in the positive arm.

These rates are a FLOOR on true-positive density, not an estimate of it: the gold corpus is only what FLoRA has already curated, so most genuine replications in any arm are necessarily absent from it. The arms' RELATIVE rates are the usable signal, and they differ by roughly two orders of magnitude between the positive and ambiguous arms.

## Skew concerns

- Partitions are OpenAlex `updated_date` shards, not publication-year shards, but a shard's contents are not publication-year-neutral: records updated in a given window skew toward recent work and toward whatever OpenAlex was re-processing. The year deciles above should be read with that in mind, and the arms compared to each other rather than to the true corpus year profile.
- Only partitions with ≥100k records were eligible, which excludes the long tail of small early-`updated_date` shards; those hold older, sparser records.
- Restricting to manifest indices ≡ 1 mod 3 (to avoid clashing with concurrent agents) is orthogonal to content but does mean the sample is not a simple random sample of partitions.
- Arm C (concept-only rescue) is defined by OpenAlex's own concept tagging, which is far denser in some fields than others; its field mix will not resemble arms A and B.
- 15–33% of every arm is OpenAlex `type == dataset` (Dataverse/Zenodo deposits, e.g. a row titled `EU27.tab`), and a further ~10–15% is `other`. These are not papers at all and will depress every arm's measured true-positive density in a way a paper-type filter would remove almost for free — worth reporting separately in the pilot rather than folding into the per-arm rate.
- Half of arm A is flagged `is_reproduction` by `keyword_verdict`, which mixes the biology/agronomy sense of 'reproduction/reproducible' into an arm otherwise carrying the precise replication phrases. Arm A is therefore two sub-populations, and a pilot that ignores the flag will average across them.
