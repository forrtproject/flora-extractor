# I4R dry run — 10 November 2026

The live [RePEc/EconPapers I4R series](https://econpapers.repec.org/paper/zbwi4rdps/)
contains **347 distinct DPs** (numbers 1–347). Exact report DOI, RePEc number or
EconStor handle matching against fred-data's `output/flora.csv` finds
**35 primary-identifier matched DPs**, corresponding to **35 FLoRA rows**, and
**312 unmatched candidates**.
The original report counted 37 matches by scanning all replication-side fields.
The reviewed implementation restricts URL/handle extraction to `url_r`, with report
DOIs matched separately in `doi_r`. DP118 and DP163 matched only through other
fields and are now conservatively reported as unmatched. They remain known
published reports; together with DP190 (SSRN) and DP208 (OSF), version-aware
published coverage is at least 39. These 312 are unmatched primary identifiers,
not 312 previously undiscovered or unscreened papers.

The count includes the entire series, including replies and methods papers;
screening determines which are eligible reproductions/replications.

The comparison used fred-data commit
[`daa16b4afe132effac0f5b7e54c537ebd21e7dbc`](https://github.com/forrtproject/fred-data/blob/daa16b4afe132effac0f5b7e54c537ebd21e7dbc/output/flora.csv).
Input CSV SHA256: `053a2a92536d694f42d78947a5f7d3d081fb041b2f144387179295d105c51e99`.
Title-only matches and alternate published versions without a shared report
identifier are outside this overlap count.

To reproduce the exact input, download the immutable commit rather than using
this checkout's older `data/flora.csv`:

```bash
mkdir -p logs
curl --fail --location --output logs/i4r-flora-2026-11-10.csv https://raw.githubusercontent.com/forrtproject/fred-data/daa16b4afe132effac0f5b7e54c537ebd21e7dbc/output/flora.csv
sha256sum logs/i4r-flora-2026-11-10.csv
.venv/bin/python -m search.i4r --flora-csv logs/i4r-flora-2026-11-10.csv --report logs/i4r-pinned-report.json
```

The checksum must be the SHA256 above. The complete DP catalogue is live, so new
source DPs after the measurement date can increase the inventory denominator.

**Originals:** 76 unmatched candidates have unique exact-title Crossref DOI hints;
236 unmatched candidates remain unresolved for Stage 3. The 35 primary-identifier
matched DPs carry their
existing FLoRA original DOI. Hints are not validated extraction results. The
harvester does not infer an original from an arbitrary DOI on a first page or in
a reference list: the existing Stage 3 target/outcome ladder reads the title,
abstract, references and full text and verifies the accepted link.

## Samples

| DP | Report title | Year | Authors | Original DOI / hint | Overlap |
|----|--------------|------|---------|---------------------|---------|
| [30](https://hdl.handle.net/10419/270965) | A (partial) replication study of Hjort and Poulsen (2019) | 2023 | Sangnier, Marc | Unresolved | New candidate |
| [334](https://hdl.handle.net/10419/344287) | A comment on "What Happens to Workers at Firms that Automate?" | 2026 | Wang, Keling; Atav, Tilbe; Bekkers, René | 10.1162/rest_a_01284 | New candidate |
| [347](https://hdl.handle.net/10419/344467) | We Just Created More Than 1,000 Synthetic Controls To Study California's $20 Fast Food Minimum Wage | 2026 | Clemens, Jeffrey; Edwards, Olivia; Meer, Jonathan; Nguyen, Joshua | Unresolved | New candidate |

The [complete 347-row inventory](../misc/i4r-inventory-2026-11-10.csv) includes
report identifiers, metadata, PDF URLs, original-title/DOI hints and overlap flags.
[Harvester operations](i4r-harvester.md) documents commands, identity and scheduling.

## Verification

- Full ordinary test suite: **2,236 passed, 6 skipped**. Subsequent targeted tests
  for harvesting, cache recovery, hint precedence, ambiguous/self DOI matches,
  checkpoint syncing and the textless tier seam pass.
- New Python files pass Ruff; `git diff --check` and systemd unit verification pass.
- Live read-only dry run: 347 total, 35 primary-identifier matches, 312 unmatched.
- Isolated `cache/i4r-review-pool`: first write publishes 312 one-row parquet shards;
  the second write reports **0 additions** and preserves all 76 original DOI hints.
- Routing that scratch pool through the complete shipped spec bundle puts all
  **312 works in `screen_expensive`**. The tier worklist and CSV handoff each read
  312 rows with `source=i4r` and no invented OpenAlex identifier.
- No paid model calls, operational-pool changes, exported production CSV writes,
  validation-table imports, or production FLoRA updates were made. The weekly timer
  is supplied but not installed. Activation after merge is in `PENDING_RUNS.md`.

After activation, Stage 2 screens and Stage 3 extracts; `extract.export` alone
renders `data/extracted.csv`. The separate flora-validation repository's
`csv_to_db.py` imports resolved records for human validation.
