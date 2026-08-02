# Pre-screen eval case sets (issue #130)

Reconstruction of the four buckets the issue's measured table was computed on. The
original harness is lost, so these are *equivalent* sets rebuilt from the same data, not
the identical rows. Built by `build_casesets.py` (deterministic — no randomness anywhere;
selection is by sorted DOI). Rebuild with:

```bash
.venv/bin/python3 analysis/prescreen_eval/build_casesets.py   # needs data/ in the main checkout
```

Every case is `{"id", "doi", "title", "abstract", "bucket", "text_source"[, "note"]}`.
`title` + `abstract` are all the pre-screen sees; both are guaranteed non-empty.

| file | id prefix | issue n | built n |
| --- | --- | --: | --: |
| `cases_goldpos_flora.json` | `GP` | 833 | **833** |
| `cases_goldpos_repro.json` | `GR` | 76 | **74** |
| `cases_goldneg_curated.json` | `NC` | 400 | **400** |
| `cases_goldneg_screen.json` | `NS` | 300 | **184** |

Total 1,491 cases (issue: 1,609). Per-bucket counts before/after each drop are in
`build_stats.json`; the four buckets are disjoint on DOI, and ids are unique across files.

## Shared construction rules

* DOIs normalised with `shared.utils.clean_doi`; one case per DOI (first row wins).
* **Stage 2 population.** One streaming pass over `data/filtered.csv` (2,581,092 rows)
  collects title/abstract for every wanted DOI. A gold DOI absent from `filtered.csv` is
  **not** in the population Stage 3 sees and is dropped.
* **Text provenance** (`text_source`): `filtered` means title and abstract both come from
  `filtered.csv`; `filtered_title+curated_abstract` means `filtered.csv` had the row and
  its title but an empty `abstract_r`, so the abstract was taken from the curated source
  CSV. This fallback is what the original eval must have done too — the Suiter abstract
  quoted verbatim in issue #130 exists only in `all_replications.csv`, not in
  `filtered.csv`. 56 / 5 / 1 cases use it in the flora / repro / curated-negative buckets.
* Buckets are made disjoint before the pass (a DOI in a positive bucket or in the
  screen-negative bucket is removed from the curated-negative bucket).
* Caps applied by sorting on DOI and taking the first N, except for the four pinned
  papers below.

## Bucket 1 — `cases_goldpos_flora.json` (GP, n=833)

`data/all_replications.csv`, `validation_status == "already_in_flora"` — rows the old
pipeline matched to an entry that is already in the human-curated FLoRA database.

1,010 rows → 974 unique DOIs → 931 in `filtered.csv` (43 dropped: not in the Stage 2
population) → 921 with a title and an abstract (10 dropped: no abstract anywhere) → capped
to 833. The cap therefore discards 88 usable cases; raise `cap` in `build_casesets.py` to
use all 921.

## Bucket 2 — `cases_goldpos_repro.json` (GR, n=74)

Curated reproductions. There is no `type` column in `data/flora_entry_sheet.csv`, so the
reproduction flag was taken from `data/all_replications.csv`: `type == "reproduction"` AND
`pathway_source != "openalex"`, i.e. reproductions that came from the curated lists
(observatory 64, politics_journals 9, lukes_list 5, education_list 3 = 81 rows) rather
than from the OpenAlex keyword harvest (809 rows, LLM-confirmed only). Union'd with
`data/reproductions.csv` (7 rows) and `data/flora.csv` rows with `type == "reproduction"`
(9 rows); DOIs already in bucket 1 removed.

84 unique DOIs → 74 in `filtered.csv` (10 dropped) → 74 with title+abstract → under the
cap of 76. **This is my best reconstruction, not a certainty**: the issue calls the source
"FLoRA entry sheet", and nothing in the entry sheet marks reproductions, so the exact 76
rows it used cannot be identified. The n landing at 74 vs 76 is consistent with this being
the right population.

## Bucket 3 — `cases_goldneg_curated.json` (NC, n=400)

`data/all_replications.csv`, `validation_status == "false_positive"` — rows the *old*
pipeline surfaced as candidate replications and then rejected. 15,137 rows → 14,150 unique
DOIs (after removing DOIs in any other bucket) → 13,952 in `filtered.csv` → 13,059 with
title+abstract → capped to 400. `note` carries the old pipeline's `prep_notes`, which is
its rejection reasoning.

**Caveat on "curated".** These labels are not human verdicts: `prep_notes` shows they were
written by the old pipeline's LLM adjudication ("openalex: The title uses 'replication' in
the sense of…"). They are high-quality negatives — mostly molecular-biology "replication",
technical/engineering "replication", or papers that merely mention replicating — but they
are *LLM-labelled* negatives, and an LLM pre-screen evaluated on them is being scored
against another LLM's judgement. I did not find any purely human-curated negative list in
`data/`. Treat the discard rates on this bucket as an upper-ish bound, and note that
because they are drawn from an LLM screen's rejects they are enriched for cases LLMs find
easy.

## Bucket 4 — `cases_goldneg_screen.json` (NS, n=184)

`data/not_a_replication.csv`, restricted to `link_method == "not_a_replication"` — rows
the *current, validated* Stage 3 `screen_gate()` discarded. This is the cleanest negative
set here: same pipeline, same population, current prompt. `note` carries `link_evidence`
or `filter_evidence`.

The file holds 501 rows today (the issue was written when it held 612); 290 are screen
discards, of which 106 have no `title_r` — they are almost all supplement DOIs of a single
PeerJ preprint (`10.7287/peerj.preprints.3153v*/supp-N`, "Prostova · 2018"), and those
rows have no title in `filtered.csv` either, so the title cannot be recovered. That leaves
**184**, not 300. I checked `extracted.csv`, `extracted-test-premerge-*.csv`,
`screen_disagreement.csv` and `target_pending.csv` for further screen discards: none.
The bucket is short of the issue's n and I did not top it up — the remaining 205
`outcome == "not_a_replication"` rows in the file were settled by *outcome coding* after a
link resolved, not by the screen, so folding them in would change what the bucket measures.

## Contamination / in-sample risk

* **Stage-3 screen derivation overlap.** 191 of the 833 GP cases share a DOI with
  `analysis/screening_eval/flora_positive_cases.json`, the derivation data for the Stage 3
  screen prompt. That is a concern only if the pre-screen prompt is tuned on those same
  rows — the pre-screen is a different prompt and different models, but if you iterate the
  pre-screen prompt against these buckets, the buckets become derivation data too, and any
  later number on them is in-sample. Hold out a slice now if you intend to iterate.
* **Bucket 4 is by construction what the current screen calls negative**, so it measures
  agreement with the screen, not with truth.
* **Bucket 3 is LLM-labelled** (see above).
* **Buckets 1 and 2 are the trustworthy positives** — human-curated FLoRA entries — and
  they are the ones the miss rate should be read off. Note issue #130's own finding that
  at least one of them (Grimm, virology) is probably a curation error, so a discard there
  is not necessarily a miss.

## The four papers issue #130 names

All four are present, all in bucket 1, and all four DOIs are **pinned** in
`build_casesets.py` (`PINNED`) so the 833 cap can never drop them:

| paper | case id | DOI |
| --- | --- | --- |
| Suiter et al. — Yale Swallow Protocol | `GP072` | `10.1007/s00455-013-9488-3` |
| Zelenski et al. — counterdispositional behaviour | `GP225` | `10.1037/a0025169` |
| Gur et al. — PFIT neuroimaging | `GP397` | `10.1093/cercor/bhaa282` |
| Grimm et al. — herpesvirus nuclear envelope breakdown | `GP527` | `10.1128/jvi.00068-12` |

`GP072`'s abstract comes from the curated fallback (`filtered.csv` has the row with no
abstract) and does open "The purpose of this prospective, double-blinded, multirater,
systematic replication study was to…", so the Suiter test the issue describes is
reproducible on this set.

`specials_raw.json` records every `filtered.csv` title matching the four keyword probes,
for auditing the matches.
