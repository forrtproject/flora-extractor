# Why 710 Metascience Observatory replications are not in the survivor pool

Measured 2026-09-21 against `cache/snapshot_pool` (2,232 parts, **5,146,160 rows**,
3,840,751 distinct DOIs, gate fingerprint `d536bc51b9b2…`, partitions
`2016-06-24 … 2026-06-26`) and against live OpenAlex. Per-work classification:
`analysis/mo_observatory/not_in_pool_causes.csv`.

Nothing in this report was inferred from code. Every count is read off the pool
parquet, off an OpenAlex record, or off a CrossRef response.

## Method

1. **Pool index.** `doi`, `id` and normalised `display_name`/`title` read from all
   2,232 parts with pyarrow (~40 s). DOI comparison after `clean_doi()`; title
   comparison after NFKD-fold → lowercase → non-alphanumeric collapse, titles ≤ 15
   chars excluded.
2. **OpenAlex.** All 710 DOIs fetched in 50-DOI `filter=doi:…` batches (15 requests,
   1× each) selecting `abstract_inverted_index`, `concepts`, `topics`,
   `publication_year`, `type`, `language`; 24 batch misses retried as single-entity
   lookups (free) and, when still absent, probed against CrossRef.
3. **Gate replay.** `REPLICATION_STEM_PATTERN` and `CONCEPT_IDS` from
   `filter/phrase_detection.py`, applied exactly as `search/snapshot_scan.py` applies
   them: the stem over `display_name`, the stem over the **raw
   `abstract_inverted_index` JSON**, and `CONCEPT_IDS` over `concepts[].id`.
4. **Coverage check.** `search/snapshot_scan.py` applies **no year bound and no type
   filter** (`_batch_rows`: "a year bound here would put rows in the pool that the
   ledger then records as a consumed partition"). The snapshot is whole. So
   hypothesis 4 — "outside the snapshot's coverage" — can only mean *the OpenAlex
   record did not exist or did not look like this on 2026-06-26*, not a scan bound.

## Cause table

| # | Cause | Works | of which already ours |
| - | ----- | ----: | --------------------: |
| A | **In the pool already — join artifact.** The Observatory DOI is malformed (`10.1023/a_1018789218611` for `10.1023/a:…`, `10.1073/pnas.2402315121 sapp`, `…%2f…`, `… (case conflict 1)`). Repaired, the DOI is in the pool. | 6 | 0 |
| B | **In the pool under a sibling record.** Same normalised title, publication year within 2, different OpenAlex record: a PsyArXiv/OSF/bioRxiv preprint (`10.31234` ×14, `10.17605` ×13, `10.1101` ×4), a publisher-duplicate DOI, or a DOI-less duplicate work (×24). 48 of the 69 matched pool rows were admitted by the **abstract** stem alone — the preprint carries an abstract the journal record lacks. | 43 | 8 |
| C | **DOI is registered nowhere.** OpenAlex 404 *and* CrossRef 404: three `10.17605/osf.io/<guid>` OSF registrations (`82bwv`, `ehjdm`, `ictp5`) and `10.30466/ijltr.2024.121418`. | 4 | 3 |
| D | **In CrossRef, absent from OpenAlex.** One work: `10.1101/2025.11.02.686051`, a Nov-2025 bioRxiv preprint OpenAlex has not ingested. (Three sibling cases — `10.21203/rs.3.rs-8351701/v1`, `10.31234/osf.io/4u98x`, `10.31730/osf.io/hxjbu` — are also absent from OpenAlex but their work is in the pool under another record, so they count in B.) | 1 | 1 |
| E | **Gate passes on today's record but the work is absent from the pool.** All 5 pass on the **abstract arm alone**; the snapshot record of 2026-06-26 must have carried no abstract (or a different one). Not provable from `updated_date` — 687 of the 694 records found have been updated since the snapshot, so that field discriminates nothing. | 5 | 0 |
| F | **Gate miss, and the gate only ever saw a title** — no abstract in OpenAlex at all. | 417 | 62 |
| G | **Gate miss with an abstract** (median 1,063 chars) that carries no replication stem. | 234 | 13 |
|   | **Total** | **710** | **87** |

Causes are assigned in the order A → B → C/D → E → F/G, so "the work is reachable in
the pool under some record" always wins over "this DOI is unknown". C and D are split
by CrossRef's status code and nothing else.

**Already ours: 87, not 92.** Computed as `doi_clean ∈` (`load_flora_skip_dois()`
over `data/flora.csv` + the FLoRA entry sheet, 2,324 keys) ∪ (`load_validated_skip()`
DOIs, 1,765). 87 hit the FLoRA list; **0** hit the validated-skip DOI list, and 0 hit
it by OpenAlex work id either. If the brief's 92 came from a different join it is
worth reconciling, but on these three files the number is 87.

**The recall gap that matters is F + G minus what is already ours: 576 works.**
A + B (49) are join defects, not recall; C + D (5, four of them already ours) are unreachable by any scan.

## The gate-miss group (F + G, 651 works; 576 net)

Median publication year 2012 (227 pre-2010, 49 pre-2000, 70 from 2020 on);
622 of 651 are `type: article`; 648 English. Disciplines of the 576 net:
psychology 225, medical fields 144, economics 70, neuroscience 63, biology 39,
linguistics 16, political science 8, other 11.

**This group is not a vocabulary problem.** It is a population whose replication
status is a *third-party judgement*, not a claim the paper makes. Typical titles:

> No association between the genetic polymorphisms within RTN4 and schizophrenia in the Chinese population
> Support for association of HSPG2 with tardive dyskinesia in Caucasian populations
> Does working memory capacity affect the ability to predict upcoming words in discourse?
> Standing enhances cognitive control and alters visual search
> Physical Activity and the Association of Common FTO Gene Variants With Body Mass Index and Obesity

Nothing in these texts distinguishes them from the several million ordinary
association studies around them. The Observatory knows they are replications because
Curate Science, FReD, a GWAS catalogue or a coding sheet said so.

### The `replication-probe` arms, measured on this population

All ten arms of `filter/spec/replication-probe.json`, applied to the OpenAlex title
and reconstructed abstract of the 651:

| Arm | Reach on the 651 | Reach on the 576 net | Can it go in the search gate? |
| --- | ---------------: | -------------------: | ----------------------------- |
| `title: \brevisit(?:ing\|ed)\b` | 11 | 9 | yes (title is a plain string in the snapshot) |
| `title: \bre-?examin\w+\b` | 6 | 6 | yes |
| `text: \ba (new\|second) look at\b` | 2 | 1 | **no** |
| `title: \bon the robustness of\b` | 1 | 1 | yes |
| `title: \breconsidered\b` | 0 | 0 | yes |
| `text: \bwe repeated the (experiment\|study\|analysis)\b` | 0 | 0 | **no** |
| `text: \ban? independent test of\b` | 0 | 0 | **no** |
| `text: \b(external\|out-of-sample) validation of\b` | 0 | 0 | **no** |
| `text: \b(attempted\|sought) to confirm\b` | 0 | 0 | **no** |
| `text: many analysts \| multi-analyst \| adversarial collaboration \| forecasting tournament` | 0 | 0 | **no** |
| **union of all ten** | **20 (3.1%)** | **17 (3.0%)** | |

The "**no**" column is structural, not a preference. The snapshot's abstract is an
`abstract_inverted_index` — a `{word: [positions]}` dictionary whose key order is
arbitrary — so only single-token tests are sound there (`_gate_masks` says exactly
this: "Do not 'optimise' this into a phrase match"). **Six of the ten probe arms are
abstract phrases and therefore cannot be search-gate arms at all.** As Stage 2 rules
they can only ever route rows the gate already admitted, which is why a `doi_in`
rule like `curated-observatory` cannot rescue any of these 710 either.

### Nineteen candidate arms, scored the same way

Nothing beat the probe arms by enough to matter. Best performers, with the
corpus-wide breadth each would add to the scan (`title.search` counts read off
OpenAlex on 2026-09-21; OpenAlex holds 200,096,238 `type:article` works):

| Candidate title arm | Net works recovered (of 576) | Works it admits corpus-wide | Works admitted per work recovered |
| ------------------- | ---------------------------: | --------------------------: | --------------------------------: |
| genetics vocabulary (`polymorphism\|snp\|allele\|haplotype\|genome-wide`) | 46 | ≥ 249,122 (`polymorphism` alone) | ≥ 5,400 |
| `comment\|reply\|rejoinder\|corrigendum` | 16 | 812,343 | 50,800 |
| `no (association\|evidence\|effect)` | 14 | 1,124,231 | 80,300 |
| `revisit(ing\|ed)` | 9 | 193,090 | 21,500 |
| `re-?examin\w+` | 6 | 16,462 | 2,700 |
| `further evidence\|support` | 2 | 10,957 | 5,500 |
| **union of all 19 candidates** | **~129 of 651 (20%)** | > 2,000,000 | |

**522 of the 651 are reached by none of the 19.**

For context on the two arms that are at least cheap, `analysis/arm_evidence.py` over
`filter/spec/replication-probe.json` (release `2e31c9543026`) scores them *within the
pool*: `revisiting` 7,404 pool rows / 7,385 exclusive / 71 FLoRA / yield 9.61 per
1,000; `re-examin*` 1,038 / 1,036 / 15 / yield 14.48. Those are respectable Stage-2
numbers. They are not search-gate numbers: as a gate arm, `revisiting` adds 193,090
works to a 5.1M pool (+3.8%) to recover 9 of these 576.

### Hypothesis 3, and why abstract enrichment will not help

417 of the 651 (64%) have **no abstract in OpenAlex**, so the gate saw a title only.
The obvious fix — enrich abstracts before gating — was tested rather than assumed.

A random sample of 120 of the 417 (`random_state=42`) was queried against the
CrossRef REST API: **0 of 120 carry an `abstract` field.** Zero lookup failures. A
control of 40 works from the *with-abstract* group returned 19 CrossRef abstracts,
so the probe works; these publishers simply never deposited one. Median publication
year of the no-abstract group is 2012 and they are dominated by pre-2015
psychology/genetics journals.

Even if abstracts existed, the with-abstract half shows what they would buy: of the
234 works that **do** have an abstract, the stem fired on none of them — that is the
definition of group G.

## Ranked recommendation

### 1. Inject the curated DOIs into the POOL, not into a Stage-2 rule — recovers 576

`curated-observatory.json` is a Stage-2 `doi_in` rule. Stage 2 routes rows that are
already in the pool, so the rule reaches **0 of these 710** by construction. The
missing piece is a Stage-1 path that takes a list of DOIs, fetches those works from
OpenAlex and appends them to the pool as rows carrying a fourth provenance flag
(`hit_curated`) beside `hit_token_title` / `hit_token_abstract` / `hit_concept`.

- **Recovers:** 576 net (623 minus the 47 in A/B/C/D that are already reachable or
  unreachable). 1,346 further Observatory works that a live routing measurement
  already showed no rule matches (commit `a90cb8b`) ride the same mechanism.
- **Cost:** 15 OpenAlex filter-query credits for 710 DOIs — this analysis paid it.
  No scan, no re-gate, no LLM call. The 576 then cost one expensive-screen pair each.
- **Caveat, and it is the real one:** the pool's identity is
  `pool_fingerprint()`, and `_pool_provenance.json` records "the gate the pool's rows
  were ADMITTED under". Rows admitted by a curated list did not pass that gate. The
  provenance sidecar has to say so, or the pool stops describing itself — which is
  exactly the failure `68cafdf` ("the FLoRA labels widen a scan's scope; they must
  never become it") was written against. Design that before writing it.

### 2. Version-aware pool matching — recovers 43 (35 net) and repairs the join

43 works are in the pool *now*, under a preprint, a publisher-duplicate DOI or a
DOI-less duplicate record. The current comparison is DOI-exact after `clean_doi()`.

- **Recovers:** 43 (35 net). More importantly it corrects the headline: the true
  "absent from the pool" figure is 661, not 710.
- **Cost:** free. Normalised title + year-within-2 found all 43 in 20 s over the pool.
  A tighter version would use each OpenAlex record's own `locations[]` DOIs.
- **Also worth fixing:** `clean_doi()` leaves `10.1073/pnas.2402315121 sapp`,
  `… (case conflict 1)`, `%2f` and `10.1023/a_1018789218611` unrepaired. Six works
  are in the pool under the repaired spelling. This is an Observatory-export defect;
  repair it on import, do not loosen `clean_doi()`.

### 3. `title: \bre-?examin\w+\b` as a search-gate arm — recovers 6

The only candidate arm whose breadth is small enough to argue about: 16,462 works
corpus-wide (+0.3% on the pool), pool yield 14.48 FLoRA-exclusive per 1,000 — the
best yield of any probe arm. Six of these 576, plus whatever it reaches outside this
sample.

- **Cost:** a re-scan of the 725 GB snapshot, because the gate fingerprint moves and
  every partition must be re-read. **That is the dominant cost and it is not small.**
  Only do this bundled with other gate changes, never for 6 works.

## Not worth it

| Candidate | Why not |
| --------- | ------- |
| `title: revisit(ing\|ed)` as a **gate** arm | 193,090 works added to a 5.1M pool to recover 9 of 576. Keep it where it is — a Stage-2 probe over rows the gate already admitted, where its 9.61/1k yield is real. |
| The six `text_regex` probe arms (`a new look at`, `we repeated the study`, `an independent test of`, `external validation of`, `attempted to confirm`, many-analysts) | Structurally impossible in the gate: the snapshot abstract is an inverted index with no word order. Combined reach on this population is 3 works anyway. |
| `comment\|reply` / `no association` / genetics vocabulary | 812k / 1.12M / 249k+ works corpus-wide for 16 / 14 / 46 recoveries. Between 5,400 and 80,300 admitted works per work recovered. These are descriptions of whole literatures, not of a genre. |
| Abstract enrichment (CrossRef) before the gate | 0 of a 120-work sample has a CrossRef abstract, against a control of 19/40. The abstracts do not exist. |
| New OpenAlex concept ids | The 651 carry no replication-adjacent concept; their top primary topics are "Neural and Behavioral Psychology Studies" (21), "Genetic Associations and Epidemiology" (16), "Reading and Literacy Development" (15) — a long flat tail of ordinary subject topics with no shared handle. |
| Chasing C + D (5 works) | 4 DOIs are registered nowhere; 1 is a preprint OpenAlex has not ingested. Nothing to find. |
| Re-scanning for group E (5 works) | They pass today on the abstract arm and were admitted by nobody on 2026-06-26; the next routine re-scan picks them up for free. |

## What is genuinely surprising

**The probe file's premise is right and its vocabulary is wrong.** `replication-probe`
was written for exactly this blind spot — "every arm names a replication genre that
carries NO replication vocabulary" — and on the largest measured sample of that blind
spot it reaches 3.0%. The genre it imagined (economics `Revisiting X`) is real but
tiny here: 9 of 576. The actual blind spot is the genetic-association and
negative-result literature of 2005–2015, where the paper is a replication only
because a curator said so.

There is no word to add. There is a list to import.
