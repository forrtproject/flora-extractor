# Reading abstracts the snapshot lacks, before the search gate — scoped and costed (2026-09-23, handover step 7)

Scripts: this folder (`.venv/bin/python -m analysis.recall_pregate.<script>`); outputs in
`cache/` (gitignored, ~0.7 GB). Read-only: no scan, route, screen or extract; no paid calls.

## Recommendation

**Don't fetch abstracts for the snapshot's no-abstract works. Run the gate over PubMed instead.**

- **Why PubMed:** all 165 recall-gap works (the Observatory's cause-F works not already ours
  whose recovered abstract fires the gate stem) have a PMID, and the Europe PMC text that
  rescued them is the MEDLINE record (200/200 sampled hits `source: MED`). The cheap source
  is the PubMed baseline — a free bulk download (50.6 GB baseline + 16.4 GB updates) — not
  Europe PMC queried per DOI.
- **How:** run the unchanged `REPLICATION_STEM_PATTERN` over PubMed title+abstract, subtract
  what the pool already holds, fetch the rest from OpenAlex by PMID.
- **Cost:** ~300k new pool rows (+6%), ~6k OpenAlex filter requests, $0, ~half a day wall
  clock. No 725 GB rescan, no narrowing rules. All 165 gap works would enter the pool.
- **The catch:** none of them would be screened under today's rule book. The two rules that
  admit on abstract text, `replication-claim-text` and `replication-claim-residual`, are
  `shadow: true`, so 0/165 reach `screen_expensive`. With them promoted, 133/165 do (the
  shipped screen had already said `proceed` on 125 of those). **Promoting those rules is the
  real switch**; the PubMed pass gives them rows to act on.

The narrowing rules asked about do work (267M → 7.8M works at 96% gap recall), but they only
matter for a per-DOI API fetch, and that design is worse even after narrowing.

## 1. What exists today

| Source | Shape | Rate | Key | Runs in | Yield on no-abstract works |
|---|---|---|---|---|---|
| OpenAlex batch | 50 ids/request | 0.3 s | key | Stage 2 backfill, opt-in | ~0 |
| Europe PMC `searchPOST` | 100 DOIs/request | 1.6 s measured, occasional 503 | none | Stage 2 backfill, bulk | 76% with PMID, 4% without (4,000-work probe) |
| OSF | 1/request | 1 s | optional | Stage 2 targeted | n/a |
| Semantic Scholar batch | 500/request | 5 s | `S2_API_KEY` (unset) | Stage 2 targeted | 12% no-PMID sample (n=1,000); 20% FLoRA no-PMID (30/148) |
| CrossRef | 1/request | 0.1 s, 429s today | none | Stage 2 targeted | 5% no-PMID (8/154), 0% PMID (0/146) |
| Scopus | 1/request | ~10k/week | key + IP | Stage 2 targeted | not probed |

- Stage 1 runs no abstract source: the scan reads only `abstract_inverted_index`. The backfill's
  only shipped worklist is `pending/no_text` rows the gate already admitted.
- A no-abstract work passes the gate only on title stem or concept. Gap titles are ordinary; even a
  very loose title regex reaches 41% of them — no title pre-filter is viable.
- The snapshot is not on disk; it streams from `https://openalex.s3.amazonaws.com/data/parquet`
  (2026-06-26 manifest: 2,446 files, 510,372,821 works).

## 2. The no-abstract universe (snapshot sample)

64 partition files, proportional to record count, one random row group each: 4,605,715 works
(0.90%). Gate replay: 0.80% pass (~4.07M, consistent with the 5.1M-row pool). No abstract: 52.3%,
~267M works (±24M); essentially none passes on title or concept. Profile: 49% DOI; article 56%,
other 19%, book-chapter 9%, dataset 6.5%; 62% English; Physical 33%, Social 28%, none 18%; 10% cite
≥1 reference; 4.5% have a PMID.

## 3. Narrowing rules, measured (cumulative; `analyse.py` → `cache/rules.csv`)

gap = 165 works; gap_proc = 148 the screen said proceed on; flora_na = 213 FLoRA/extracted
replications with no OpenAlex abstract; flora_sim = all 4,487 FLoRA/extracted replications with the
abstract ignored.

| Predicate | Kept | gap | gap_proc | flora_na | flora_sim |
|---|--:|--:|--:|--:|--:|
| universe | 267M | 165 | 148 | 213 | 4,487 |
| `doi IS NOT NULL` | 132M | 165 | 148 | 213 | 4,487 |
| `type IN (article, preprint, review, report)` | 56M | 162 | 145 | 207 | 4,307 |
| `NOT is_paratext AND NOT is_retracted` | 56M | 162 | 145 | 207 | 4,307 |
| `referenced_works_count > 0` | 18.4M | 161 | 144 | 172 | 3,898 |
| `primary_topic.domain IN (Social, Life, Health)` | 9.3M | 159 | 142 | 164 | 3,685 |
| `publication_year >= 1970` | 8.5M | 159 | 142 | 164 | 3,669 |
| `language = 'en' OR NULL` | **7.8M ±2.9M** | **159** | **142** | 164 | 3,664 |
| `ids.pmid IS NOT NULL` | 3.6M ±2.7M | 159 | 142 | **63** | 2,226 |

- The first seven rules cut volume 34× for a 4% loss on the gap set. `refs > 0` is the most
  efficient single rule (keeps 10% of the universe, 99% of the gap).
- Requiring a PMID costs nothing on the gap set but loses 61% of FLoRA's own no-abstract
  replications (Elsevier/Springer social science and economics), which no cheap source recovers
  (Europe PMC 1%, CrossRef 2%, Semantic Scholar 20%).
- Yield in the 7.8M: PMID part — Europe PMC returns 76%, 2.35% fire the stem (~85k admits); the
  rest ~13k.

## 4. The inversion: run the gate over PubMed

5 baseline files across the PMID range (150,000 citations; `pubmed_probe.py`): 84% have an
abstract, 3.4% fire the stem (5,040 hits).

| A PubMed stem hit is… | Per 150k | Full baseline (×266.8) |
|---|--:|--:|
| in the pool, OpenAlex has the abstract | 3,548 | — |
| in the pool on title/concept, OpenAlex has no abstract | 335 | **~89k** pool rows get text free |
| **not in the pool; gate fails on the OpenAlex record** | 1,148 | **~300k** (219k–351k across files) |
| not in OpenAlex at all | 9 | ~2k |

New hits by crude sense (n=1,133): molecular/viral replication 42%, measurement reproducibility /
technical replicates 23%, study-replication wording 5%, other 30% — like the pool's existing mix.

Stage 2 routing in memory (`route_offline.py`, live bundle minus `curated-observatory`): live
bundle → 0/165 gap and 0/1,132 new PubMed rows reach `screen_expensive`. With the two
abstract-claim rules promoted → 133/165 gap, 34/1,132 PubMed rows (3.0%, ~9k over the baseline,
~$25 at the dry-run price of ~$0.0025/work). Everything else lands in `no_filter_matched`.

| | A. PubMed inversion (recommended) | B. Narrowed per-DOI fetch |
|---|---|---|
| Input list | none | 7.8M works; needs a snapshot pass first |
| Fetching | 67 GB streamed; ~4–14 h (~1.3 MB/s per stream) | 78k Europe PMC requests × 1.6 s ≈ 35 h; CrossRef not worth it |
| Parsing | 12.4 s/file, ~5.6 CPU-h (~45 min on 8 cores) | trivial |
| OpenAlex requests | ~6k filters on `ids.pmid` | none |
| New pool rows | ~300k | ~98k |
| Gap works reaching the pool | 165/165 | 159/165 |
| Money | $0 | $0 |

B only wins on the non-PubMed remainder, where sources barely answer; there the same inversion
applies to the Semantic Scholar bulk `abstracts` dataset (needs a key; not measured).

## 5. Where it plugs in

Not inside `snapshot_scan` — a new gate arm changes `search_gate_fingerprint()` and forces a 725 GB
rescan. Instead a separate Stage 1 pass (e.g. `search/pubmed_scan.py`):

1. Stream baseline + update files with a per-file ledger; keep citations matching
   `REPLICATION_STEM_PATTERN` (last version of a PMID wins; `DeleteCitation` removes).
2. Drop works already in the pool (join on `id`); fetch the rest by `ids.pmid`; write
   `_POOL_SCHEMA` rows as `supplement-pubmed-<release>-<seq>.parquet` with `abstract_text` = the
   PubMed text and `hit_token_abstract = True`.
3. Add a `supplements` list to `_pool_provenance.json` (corpus, PubMed release, gate fingerprint,
   files, rows) and count those files in `expected_files`. `pool_fingerprint()` already hashes
   every `*.parquet`, so a supplement mints a new pool fingerprint and release id.
4. The ~89k existing pool rows without an abstract get PubMed text through an overlay chunk
   (source `pubmed`), moving `overlay_hash`, not the pool.
5. Redo the subtraction after any snapshot rescan so a work that later gains an OpenAlex abstract
   is not in the pool twice.

This matches the intended split: the bulk source runs unlimited in Stage 1; Stage 2's backfill keeps
the restricted sources (OSF, Semantic Scholar, CrossRef, Scopus) for the few thousand `no_text` rows.

## Open questions for Lukas

1. Promote `replication-claim-text` and `-residual`? Without that, this admits rows nobody screens.
   Their spec counts 89,113 pool rows for the twelve arms vs 7,760 in `screen_expensive` today; a
   narrower option is a live copy scoped to the supplement rows only.
2. Supplement rows in the pool directory (with the sidecar extension), or a separate release input
   like the overlay?
3. Chase the non-PubMed remainder? 70% of FLoRA's own no-abstract replications are not in PubMed;
   Semantic Scholar recovers ~20% and needs a key.
4. Snapshot-side volumes are from a 64-cluster sample (wide intervals); the PubMed numbers are
   steadier (164–263 new hits per file), and the build itself gives the exact count.

## Reproduce

`build_f_group` (417 cause-F works → the 165 gap set); `sample_snapshot --files 64 --workers 6`
(~45 min HTTPS range reads); `fetch_positives` (~110 OpenAlex filters); `probe_sources --which
flora_noabs --crossref-n 213`; `probe_sources --which sample_noabs --n 2000 --crossref-n 300`;
`analyse`; `pubmed_probe` (5 baseline files, ~100 OpenAlex requests); `pubmed_hits_profile`;
`route_offline` (in-memory routing). The two keyless Semantic Scholar probes are in
`cache/probe_s2_*.json`.
