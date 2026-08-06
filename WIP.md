# WIP — the Stage 3 CSV runner (parked)

This branch preserves `extract/run_extract.py`'s **orchestration half** — the CSV
runner — exactly as it stood on `main` at the moment the authority flip removed it.
Nothing else on this branch differs from that commit.

The per-row pipeline (`_process_row`, `_resolve_and_code`, the row builders, the
guard, `_verify_row`/`_finalise_row`) is NOT parked: it survives on `main` as library
code, and it is what `extract/tier.py` calls. Only the run loop around it was removed.

## What the CSV runner was

`python -m extract.run_extract` read `data/filtered.csv` in 50k-row chunks, decided
per row whether to skip it, ran the ladder on `EXTRACT_WORKERS` threads, and appended
each finished row to `data/extracted.csv` under a write lock. Around that sat:

- **a file-based resume** — `_load_extracted_rows` / `_load_and_truncate` read the
  output CSV back, partitioned it by `row_key()` into resolved and pending, truncated
  the file to its header, wrote the resolved rows straight back (re-verifying the
  unsettled `doi_o_verification` values), and carried the untouched pending rows back
  in a `finally`;
- **`--fresh`**, which discarded that state and re-paid for everything;
- **`--rescreen`**, which reopened the papers a previous run had filed in the
  screen-verdict set-aside CSVs;
- **a manifest handshake** — `_verify_input_manifest` / `_file_sha256` checked
  `filtered.csv.manifest.json` against the file on disk, and `_require_screen_verdicts`
  refused an input with no `screen_verdict` column;
- **`--screen-here`**, the fallback that ran the front-door screen in Stage 3 for
  inputs carrying no verdict;
- **`--extracted-test`** plus `extract/promote_test.py`, the sandbox: write to
  `data/extracted-test.csv`, skip DOIs already resolved in production, promote rows
  later.

## Why it was removed from `main`

Stage 3 now runs as the claimed `extract` engine tier (`extract/tier.py`). The
permanent verdict row in Postgres is the checkpoint, its payload rebuilds every
`EXTRACTED_COLS` row offline, and `python -m extract.export` renders those payloads
into `data/extracted.csv`. That makes the export the **only** writer of that file.

Two writers of one authoritative CSV is the problem, not the runner's own quality:

1. **The checkpoint would be ambiguous.** The tier's resume is the verdict row; the
   runner's resume was the output file. Running both means a paper can be "done"
   according to one and open according to the other, and the two disagree silently.
2. **The runner cannot record what it spent.** It has no claim, no budget gate and no
   evidence rows, so a row it extracted has no lineage — which is the whole point of
   moving Stage 3 onto the engine spine.
3. **Its inputs are no longer the contract.** The runner's distinguishing feature was
   accepting a hand-made CSV or an `--as-routed` handoff, screening it in place with
   `--screen-here`. Such rows carry no `work_id`, so they cannot be claimed, cannot
   produce a verdict row and cannot be exported. The current contract is
   OA-pool-only inputs, claimed through the tier.
4. **It was one-shot and non-authoritative in practice.** Every campaign it ran was
   reconstructed afterwards from the CSV it happened to leave behind.

The sandbox went with it: `claim --mode validation` plus
`python -m extract.export --mode validation --out data/extracted-test.csv` gives the
same isolation, from real verdicts, with no promotion step — a validation-mode run's
rows are already recorded and simply invisible to the live file.

## What would make it correct to revive

Reviving the runner as a *second front door* is defensible; reviving it as a second
*writer* is not. Two conditions:

1. **It must refuse to write `data/extracted.csv`.** Give it an output path it cannot
   default to production — a scratch CSV named on the command line — and make writing
   the authoritative file an error. `extract.export` is that file's only writer.
2. **It must reuse the shared row builders, not copies of them.** Everything below
   `_process_row` already lives on `main`; a revived runner imports it. If a fix has
   to be made in two places to change what a row says, the runner has forked the
   pipeline and is worth less than the CSV it writes.

Beyond those, the honest use case is narrow: a row that has no OpenAlex work id and
therefore cannot be claimed. If that case reappears, the cheaper fix is usually to get
the work into the pool rather than to run a second pipeline beside the engine.
