# I4R dry run — 10 November 2026

The live [RePEc/EconPapers I4R series](https://econpapers.repec.org/paper/zbwi4rdps/)
contains **347 distinct DPs** (numbers 1–347). Exact report DOI, RePEc number or
EconStor handle matching against fred-data's `output/flora.csv` finds **37 already
represented DPs**, corresponding to **37 FLoRA rows**, and **310 new candidates**.
The count includes the entire series, including replies and methods papers;
screening determines which are eligible reproductions/replications.

The comparison used fred-data commit
[`daa16b4afe132effac0f5b7e54c537ebd21e7dbc`](https://github.com/forrtproject/fred-data/blob/daa16b4afe132effac0f5b7e54c537ebd21e7dbc/output/flora.csv).
Input CSV SHA256: `053a2a92536d694f42d78947a5f7d3d081fb041b2f144387179295d105c51e99`.
Title-only matches and alternate published versions without a shared report
identifier are outside this overlap count.

**Originals:** 76 new candidates have unique exact-title Crossref DOI hints;
234 new candidates remain unresolved for Stage 3. The 37 matched DPs carry their
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

- Full ordinary test suite: **2,198 passed, 6 skipped**. Subsequent targeted tests
  for harvesting, original-hint persistence, threading and the textless tier seam pass.
- New Python files pass Ruff; `git diff --check` and systemd unit verification pass.
- Live read-only dry run: 347 total, 37 existing, 310 new.
- Isolated `cache/i4r-scratch-pool`: first write publishes 310 one-row parquet shards;
  the second write reports **0 additions** and preserves all 76 original DOI hints.
- Routing that scratch pool through the complete shipped spec bundle puts all
  **310 works in `screen_expensive`**. The tier worklist and CSV handoff each read
  310 rows with `source=i4r` and no invented OpenAlex identifier.
- No paid model calls, operational-pool changes, exported production CSV writes,
  validation-table imports, or production FLoRA updates were made. The weekly timer
  is supplied but not installed. Activation after merge is in `PENDING_RUNS.md`.

After activation, Stage 2 screens and Stage 3 extracts; `extract.export` alone
renders `data/extracted.csv`. The separate flora-validation repository's
`csv_to_db.py` imports resolved records for human validation.
