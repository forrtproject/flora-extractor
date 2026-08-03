# Exclusion patterns: two-sided confusion analysis (issue #144)

Measured 2026-08-03 on branch `feat/keyword-ladder`, read-only. Nothing in the repo was
modified; proposed edits appear as diffs at the end.

## Data and method

| Set | n | Source |
| --- | --- | --- |
| Gold positives | 7,505 | `analysis/prescreen_eval/override_positives.json` (FLoRA replication/reproduction papers with title+abstract) — present in the `issue-130-prescreen` worktree |
| Gold negatives | 1,333 | `override_negatives.json` — 1,150 curated false positives + 183 screen discards (the 400/184 case files are subsets of these) |
| Real population | 5,604,314 OpenAlex snapshot rows | 22 parquet partitions from `cache/snapshot/manifest.json` (16 large recent + 6 strided older). 64,118 carried a `REPLICATION_STEM_PATTERN` token (Stage A survivors); 3,785 of those fired an exclusion pattern |

Every Stage-A-surviving snapshot row on which any exclusion fires was written to
`excl_rows.jsonl`, so the candidate narrowings could be re-scored offline. Because every
candidate only *relaxes* exclusions, that harvest is the complete set of rows any
candidate can move — the influx numbers are exact for the scanned population, not sampled.

Baseline admission rate on the scanned population: **2,311 rows/million** are admitted
today (4,181 positive + 7,441 ambiguous per 5.03M rows). Influx below is quoted per
million scanned, as a percentage of that admitted volume, and projected onto the whole
snapshot (510,372,821 rows in the manifest).

`x_candidates.verdict()` reproduces production `keyword_verdict()` exactly on all 7,505
gold positives (0 mismatches) before any patch is applied.

## 1. Recompute — the 121 killed gold positives

Confirmed, pattern for pattern, against issue #144:

| Pattern | Gold positives killed (`negative`) | Gold positives held at `ambiguous` by the #44 phrase+cite rescue | Gold negatives killed | Fires anywhere on gold-pos |
| --- | ---: | ---: | ---: | ---: |
| BIOLOGICAL_OF | 36 | 2 | 72 | 49 |
| TECHNICAL_OBJECT | 25 | 5 | 0 | 30 |
| BIOLOGICAL | 18 | 1 | 13 | 19 |
| TECHNICAL_VERB | 17 | 7 | 0 | 26 |
| EDITORIAL_ARTIFACT | 15 | 8 | 1 | 23 |
| DATA_AVAILABILITY | 8 | 4 | 0 | 12 |
| STRUCTURAL | 2 | 0 | 0 | 3 |
| **total** | **121** | **27** | **86** | |

(Gold-positive verdicts overall: 6,921 positive, 144 ambiguous, 440 negative — so the
exclusions account for 121 of the 440 negatives; the other 319 are the vocabulary
misses another workstream is handling.)

What the 121 actually are, read individually:

* **BIOLOGICAL_OF (36)** — 24 are the GWAS locus-replication genre ("Replication of
  genome-wide association studies (GWAS) loci for fasting plasma glucose in
  African-Americans"), where the killer token is `genome`/`genomic` inside
  *genome-wide*. The other 12 are genuine virology/cell-biology papers that sit in the
  FLoRA corpus (HIV, HCV, dengue/Wolbachia, "replication of transfected plasmids").
* **TECHNICAL_OBJECT (25)** — 12 are real in-scope studies whose subject is a *model*
  ("A Replication and Analysis of Tiebout Competition Using an Agent-Based Computational
  Model", "Cross-Model Replication Study…", "Replicating MOOC predictive models at
  scale"); 4 are Dataverse/Zenodo *deposits* titled "Replication Data for: …"; the rest
  fire on a passing "replication of the data/method" phrase inside the abstract of an
  ordinary study.
* **BIOLOGICAL (18)** — all 18 are molecular/virological ("Virus Replication", "DNA
  replication", "β-Cell Replication"). These are FLoRA rows whose replication paper is
  itself a biology paper.
* **TECHNICAL_VERB (17)** — 15 are social-science/CS replication studies whose abstract
  says "we replicated the model / replicated the data / replicated the method"
  ("Social Capital and Value Creation: A Replication of 'The Role of Intrafirm
  Networks'"). This is the pattern with the highest share of clear misfires.
* **EDITORIAL_ARTIFACT (15)** — 13 `10.7287/…/reviews/N` PeerJ peer-review records, 1
  Faculty Opinions record, 2 eLife/PCI author responses. The DOIs are the review
  records' own DOIs, not the papers'. The pattern is right; the corpus row is the artifact.
* **DATA_AVAILABILITY (8)** — 7 are dataset deposits (openICPSR `10.3886/…`, Zenodo,
  Dataverse). One is a real paper: `10.1186/s12916-020-01518-9` "Non-confirming
  replication of 'Performance of InSilicoVA…'", killed by "code necessary to replicate"
  in its data-availability sentence.
* **STRUCTURAL (2)** — both are molecular biology (replication fork, replication timing).

## 2. What each pattern is protecting — measured on 5.6M real rows

"Marginal suppression" = Stage-A-surviving rows that are `negative` today and would stop
being negative if that pattern alone were deleted. That is the pattern's actual
protective value, net of the other patterns.

| Pattern | fires (first) | suppressed → negative | marginal suppression | per million scanned | eyeballed sample (20): plausibly in-scope |
| --- | ---: | ---: | ---: | ---: | --- |
| BIOLOGICAL | 3,034 | 3,028 | 543 | 96.9 | 0/20 — all virology/molecular ("SARS-CoV-2 infection and replication in human gastric organoids") |
| BIOLOGICAL_OF | 300 | 293 | 292 | 52.1 | 0/20 — virology plus non-English noise whose abstract mentions viral replication |
| STRUCTURAL | 186 | 183 | 65 | 11.6 | 0/20 — replication fork / replication stress / origins, uniformly |
| DATA_AVAILABILITY | 96 | 88 | 45 | 8.0 | 0/20 — dataset deposits, "Replication package for …", supplementary files |
| TECHNICAL_OBJECT | 62 | 62 | 31 | 5.5 | ~1/20 — distributed-systems data/database replication dominates ("A Strategy for Data Replication in Mobile Ad Hoc Networks") |
| TECHNICAL_VERB | 68 | 68 | 18 | 3.2 | 3/18 — CRDT/quorum "replicated data" CS, but also "A Pre-Registered Test of a Correlational Micro-PK Effect: Efforts to Learn from a Failure to Replicate" and "External validation of existing dementia prediction models" |
| EDITORIAL_ARTIFACT | 39 | 38 | 16 | 2.9 | 0/16 — Review for "…", Decision letter for "…", Peer Review #N, Faculty Opinions, Correction to |

Matched-span census over the whole harvest tells the same story from the other side:
`TECHNICAL_OBJECT` fires on `data replication` (36), `database replication` (7),
`model replication` (3); `TECHNICAL_VERB` on `replicated/replicating/replicate data`
(35), `replicated database` (5), `replicating the model` (5). The dominant real-world
referent of both patterns is storage replication, not research methodology.

**Only 2 of the 300 BIOLOGICAL_OF firings in 5.6M rows were the GWAS genre** — both
were genuine GWAS-locus replication papers ("Replication of Genome-Wide Association
Studies of Type 2 Diabetes Susceptibility in Japan"). The GWAS clause of BIOLOGICAL_OF
is almost pure false-positive suppression *within FLoRA's own scope definition*: it
catches the gold genre 24 times and catches nothing else 0.4 times per million rows.

## 3. Candidate narrowings, priced on both sides

Full table in `exclusion_candidates.csv`. Sorted by gold positives recovered per 1,000
extra rows admitted across the full snapshot.

| Candidate | Change | +gold pos (pos/amb) | +gold neg | influx /M | % of admitted volume | projected extra rows (510M) | gold per 1k extra |
| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: |
| TO4_title_rescue | TECHNICAL_OBJECT: abstract-only match + title phrase → ambiguous | 4 (0/4) | 0 | 0.0 | 0.00% | 0 | free |
| DA1_title_rescue | DATA_AVAILABILITY, same rule | 3 (0/3) | 0 | 0.0 | 0.00% | 0 | free |
| BO2_gwas_demote | BIOLOGICAL_OF match containing genome-wide/GWAS → ambiguous | 21 (0/21) | 2 | 0.4 | 0.02% | 204 | 102.9 |
| BO1_gwas_exempt | `genome(s)`/`genomic` no longer match when followed by `-wide` | 20 (20/0) | 2 | 0.4 | 0.02% | 204 | 98.0 |
| TV3_title_rescue | TECHNICAL_VERB, title-phrase rule | 8 (0/8) | 0 | 0.2 | 0.01% | 102 | 78.4 |
| COMBO_B | TV1 + title rescue on TO/BO/BI/DA + GWAS demotion | 42 (13/29) | 6 | 8.0 | 0.35% | 4,083 | 10.3 |
| TO2_both_tight | TECHNICAL_OBJECT both sides → apparatus\|code\|database\|pipeline\|protocol\|software\|simulation | 18 (18/0) | 0 | 3.9 | 0.17% | 1,990 | 9.0 |
| TV4_drop | TECHNICAL_VERB deleted | 14 (14/0) | 0 | 3.2 | 0.14% | 1,633 | 8.6 |
| TV1_tight_objects | TECHNICAL_VERB objects → apparatus\|code\|software\|pipeline\|script\|database | 13 (13/0) | 0 | 3.0 | 0.13% | 1,531 | 8.5 |
| TO1_prefix_tight | drop model/data/dataset/method from the "X replication" side only | 12 (12/0) | 0 | 3.0 | 0.13% | 1,531 | 7.8 |
| BO3_title_rescue | BIOLOGICAL_OF title-phrase rule | 2 (0/2) | 3 | 0.5 | 0.02% | 255 | 7.8 |
| COMBO_A | TV1 + title rescue on TO/BO/BI/DA | 23 (13/10) | 4 | 7.7 | 0.33% | 3,930 | 5.9 |
| TO3_demote | TECHNICAL_OBJECT → ambiguous | 25 (0/25) | 0 | 11.1 | 0.48% | 5,665 | 4.4 |
| ED1_demote | EDITORIAL_ARTIFACT → ambiguous | 15 (0/15) | 1 | 6.8 | 0.29% | 3,471 | 4.3 |
| TV2_demote | TECHNICAL_VERB → ambiguous | 17 (0/17) | 0 | 12.1 | 0.52% | 6,176 | 2.8 |
| DA2_demote | DATA_AVAILABILITY → ambiguous | 8 (0/8) | 0 | 15.7 | 0.68% | 8,013 | 1.0 |
| BI1_title_rescue | BIOLOGICAL title-phrase rule | 1 (0/1) | 1 | 4.1 | 0.18% | 2,093 | 0.5 |
| ST1_demote | STRUCTURAL → ambiguous | 2 (0/2) | 0 | 32.7 | 1.41% | 16,689 | 0.1 |

"+gold neg" counts rows of the 1,333-row gold-negative set that stop being `negative`.
All such admissions land on `ambiguous`/`positive` and would then have to be paid for
downstream; none of the candidates admits more than 6.

Influx composition, sampled:

* **TV1 / TV4** (17–18 rows / 5.6M): mostly distributed-systems papers whose title
  itself carries a stem ("Using Conflict-free Replicated Data Types to support Block
  Editing", "Managing Replicate Data in JASMIN") — they enter as `ambiguous`, not
  `positive`. 6 enter as `positive`, of which one is genuinely in scope ("A
  Pre-Registered Test of a Correlational Micro-PK Effect: Efforts to Learn from a
  Failure to Replicate").
* **TO1 / TO2** (17–22 rows): almost entirely "Data Replication in Mobile Ad Hoc
  Networks"-style CS papers and one Zenodo replication package.
* **BO1 / BO2** (2 rows): both are GWAS-locus replication papers, i.e. the same genre
  as the gold rows they recover.
* **ED1** (38 rows): all peer-review records, decision letters, Faculty Opinions,
  corrections — exactly what the pattern exists to remove.
* **DA2** (88 rows) and **ST1** (183 rows): dataset deposits and molecular-biology
  replication-fork papers respectively; no in-scope material in the samples.

## 4. Ranking and verdicts

**Clearly good trades**

1. **Title-phrase rescue for TECHNICAL_VERB, TECHNICAL_OBJECT and DATA_AVAILABILITY**
   (`TV3 + TO4 + DA1`): if the exclusion matched only past the title/abstract join and
   the TITLE itself carries a `REPLICATION_PHRASES` match, demote to `ambiguous`
   instead of `negative`. Recovers 15 gold positives; total measured influx is **1 row
   in 5.6M** (0.2/M), and every recovered row goes to `ambiguous`, i.e. the LLM screen
   decides rather than the keyword rule. This is the cheapest available recovery and it
   defers rather than admits.
2. **BIOLOGICAL_OF: stop `genome`/`genomic` matching inside `genome-wide`** (`BO1`, or
   `BO2` if a demotion is preferred). Recovers 20–21 gold positives (17% of the 121) for
   0.4 extra rows per million — ~204 rows over the entire 510M-row snapshot — and 2 gold
   negatives. The clause is currently doing near-zero protective work on real data.
   *Caveat, and it is a scope question, not a measurement one:* FLoRA's own gold set
   contains 24 GWAS locus-replication papers as positives, while `_GWAS_GUARD` in
   `phrase_detection.py` deliberately suppresses first-person GWAS replication claims
   (59 curated negatives removed at zero gold cost). The two rules disagree about this
   genre. If GWAS locus replication is in scope, BO1 is a clear win; if it is not, the
   gold corpus is what needs fixing, not the pattern.

**Judgment calls**

3. **TECHNICAL_VERB object list** (`TV1`, 13 gold, 3.0/M) or deleting the pattern
   outright (`TV4`, 14 gold, 3.2/M). The pattern's whole protective yield is 18 rows per
   5.6M and 3 of 18 sampled suppressions looked in-scope, so its precision is the worst
   of the seven. But TV3 already recovers 8 of the 13 for free, so TV1 buys 5 more gold
   rows for ~1,500 extra rows across the snapshot. Worth doing only if the extra 5 matter.
4. **TECHNICAL_OBJECT object list** (`TO2`, 18 gold, 3.9/M; `TO1`, 12 gold, 3.0/M). The
   influx is CS storage-replication papers that would enter as `ambiguous` and be
   discarded by the screen at LLM cost. `TO2` is the better of the two (more gold, same
   order of influx) because it removes `model`/`data`/`method` from both sides.
5. **TO3 / TV2 blanket demotions to `ambiguous`** (25 and 17 gold): these recover the
   most gold of any single-pattern change, but 11–12 rows/M is 5,000–6,000 extra LLM
   candidates across the snapshot to recover ~40 known papers. Defensible only if
   Stage 3 capacity is not the binding constraint.

**Leave alone — the record, not the pattern, is the odd one out**

6. **EDITORIAL_ARTIFACT.** All 15 gold "kills" are peer-review records, Faculty Opinions
   entries and author responses with their own DOIs; the underlying papers have separate
   records that the pipeline reaches normally. Demoting the pattern costs 3,471 extra
   rows across the snapshot, 100% of them the same artifact class. The fix, if any, is
   in how the gold corpus resolved those DOIs.
7. **DATA_AVAILABILITY (blanket).** 7 of 8 gold kills are Zenodo/Dataverse/openICPSR
   deposits. `DA2` costs 15.7/M for them. The one real paper is recovered free by `DA1`.
8. **STRUCTURAL.** 2 gold kills, both genuine molecular biology; demotion costs 32.7/M —
   the worst ratio measured (0.1 gold per 1,000 extra rows).
9. **BIOLOGICAL.** 18 gold kills, all genuinely biological papers; `BI1` recovers 1 for
   4.1/M. The pattern is the single largest protective element in the set (543 marginal
   suppressions per 5.6M) and every sampled suppression was out of scope.

## 5. Proposed diffs

Only two of the recommended changes touch `filter/spec/exclusion-patterns.yaml`; the
title-phrase rescue is a `keyword_verdict()` change, since the YAML has no verdict
vocabulary.

### 5a. BIOLOGICAL_OF — exempt `genome-wide` (recommendation 2)

```diff
   - id: BIOLOGICAL_OF
-    regex: '\breplication of (?:the\s+)?(?:(?!(?:study|studies|experiments?|effects?|findings?|results?|analys[ei]s|papers?|trials?|surveys?)\b)\w+[-\s]+){0,3}(?:(?:dna|rna|mrna|cdna|viral|\w*virus(?:es)?|bacteri\w+|mycobacteri\w+|pathogens?|prions?|phages?|chromosomes?|plasmids?|genomes?|genomic|hiv|sars[-\s]?cov[-\s]?2?|influenza|mitochondrial|organoids?)\b|(?:cells?|cellular|parasites?)\b(?=\s*(?:[.,;:)\]]|$|\b(?:in|by|during|within|with|and|or|is|are|was|were|from|to|at|under)\b)))'
+    regex: '\breplication of (?:the\s+)?(?:(?!(?:study|studies|experiments?|effects?|findings?|results?|analys[ei]s|papers?|trials?|surveys?)\b)\w+[-\s]+){0,3}(?:(?:dna|rna|mrna|cdna|viral|\w*virus(?:es)?|bacteri\w+|mycobacteri\w+|pathogens?|prions?|phages?|chromosomes?|plasmids?|genomes?(?![-\s\u2010-\u2015]?wide)|genomic(?![-\s\u2010-\u2015]?wide)|hiv|sars[-\s]?cov[-\s]?2?|influenza|mitochondrial|organoids?)\b|(?:cells?|cellular|parasites?)\b(?=\s*(?:[.,;:)\]]|$|\b(?:in|by|during|within|with|and|or|is|are|was|were|from|to|at|under)\b)))'
```

The unicode dash class matters: several of the 24 GWAS gold rows write "Genome\u2010Wide"
with U+2010, so an ASCII-only `(?![-\s]?wide)` lookahead misses them. The diffed line
was loaded through `yaml.safe_load` + `re.compile` and verified: it no longer matches
"Replication of Genome-Wide/Genome\u2010Wide/Genome Wide association studies", and still
matches "replication of the viral genome in host cells" and "Replication of HIV-1".

### 5b. TECHNICAL_VERB / TECHNICAL_OBJECT object lists (recommendations 3–4, optional)

```diff
   - id: TECHNICAL_OBJECT
-    regex: '\b(?:replication of (?:the )?(?:apparatus|code|dataset|data|database|model|method|pipeline|protocol|software|simulation)|(?:apparatus|code|dataset|data|database|model|method|pipeline|protocol|software|simulation)\s+replication)\b'
+    regex: '\b(?:replication of (?:the )?(?:apparatus|code|database|pipeline|protocol|software|simulation)|(?:apparatus|code|database|pipeline|protocol|software|simulation)\s+replication)\b'

   - id: TECHNICAL_VERB
-    regex: '\breplicat(?:e|ed|ing)\s+(?:the )?(?:apparatus|code|dataset|data|database|model|method|pipeline|protocol|software|simulation)\b'
+    regex: '\breplicat(?:e|ed|ing)\s+(?:the )?(?:apparatus|code|software|pipeline|script|database)\b'
```

### 5c. Title-phrase rescue in `keyword_verdict()` (recommendation 1)

Sketch, in `filter/phrase_detection.py`, inside the `if excl:` branch after the #44
readmission check — scoped to the three patterns measured, and requiring the exclusion
to have matched past the title:

```python
_TITLE_RESCUE_EXCLUSIONS = {"TECHNICAL_VERB", "TECHNICAL_OBJECT", "DATA_AVAILABILITY"}

# The exclusion fired in the ABSTRACT while the TITLE states the design outright
# ("A Replication of 'The Role of Intrafirm Networks'" whose abstract says "we
# replicated the model"). Hand the row on as ambiguous rather than kill it: measured
# cost is 1 extra row per 5.6M snapshot rows, against 15 known FLoRA papers.
if excl in _TITLE_RESCUE_EXCLUSIONS and match_start >= len(title or "") \
        and find_replication_phrase_span(title or "", ignore_exclusions=True):
    return KeywordVerdict("ambiguous", f"exclusion:{excl}; title phrase — LLM review",
                          text, exclusion=excl)
```

This needs `is_non_scholarly_context()` to return the match position as well as the
pattern id (or a small sibling helper), since the current signature returns only the id.

## Files

* `exclusion_candidates.csv` — every candidate with both sides of the price
* `x_gold_results.json` / `x_snapshot_results.json` — full results incl. sample titles
* `gold_pos_killed.json` / `gold_neg_killed.json` — the 121 and 86 killed rows
* `excl_rows.jsonl` — the 3,785 harvested snapshot rows (title, abstract, pattern)
* Scripts: `gold_excl.py`, `harvest_excl.py`, `x_candidates.py`, `x_eval_gold.py`,
  `x_eval_snapshot.py`, `x_extra.py`, `x_make_csv.py`
