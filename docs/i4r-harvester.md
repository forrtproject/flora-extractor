# I4R Discussion Paper harvester

[Measured inventory and dry-run results](i4r-dry-run.md).

I4R candidates enter the same survivor pool as the OpenAlex snapshot. They bypass
keyword discovery and cheap screening, including repository-object DOI discards;
the expensive screen still decides eligibility. The series contains comments,
author replies and methods papers as well as reproductions: a new DP is a new
candidate, not automatically a new FLoRA reproduction.

The source is the complete [EconPapers series](https://econpapers.repec.org/paper/zbwi4rdps/)
(RePEc:zbw:i4rdps). Numbered pagination is refreshed on every run. Per-paper citation
metadata supplies the title, authors, publication year, RePEc handle and any report
DOI, plus the EconStor download and persistent handle. Successful responses are
cached before parsing; a failed detail fetch fails the run without marking that DP
done. Empty/unparseable catalogue pages and malformed details raise errors.
A nonempty truncated catalogue cannot be proven complete by this scraper; the
nonempty-page guard is not a guarantee against source-side omissions. Invalid
detail/Crossref cache bodies are evicted; a bad legacy entry is refetched in the
same call. Malformed/unavailable Crossref data leaves Stage 3 to resolve the target.

```bash
# Read-only inventory against today's fred-data output/flora.csv:
.venv/bin/python -m search.i4r
# Optional comparison with the checkout's local FLoRA copy (which may be older):
.venv/bin/python -m search.i4r --flora-csv data/flora.csv
# Exact pinned-input reproduction commands: see docs/i4r-dry-run.md.
# Write only new candidates to the configured survivor pool:
.venv/bin/python -m search.i4r --write
```

The report is `logs/i4r.json`: full metadata, overlap and new counts, original-title
and DOI hints, and the SHA256 of the compared FLoRA CSV. FLoRA overlap uses exact
report DOI from `doi_r`, or RePEc DP number/EconStor handle extracted only from
`url_r` landing/PDF URLs,
including URL-encoded links. Multiple FLoRA target rows can belong to one DP.
Counts do not include title-only matches or external published versions with no
shared report identifier.

Originals named in quoted report titles are looked up in Crossref. Only a unique
exact normalized title match supplies `doi_o`; it remains a bibliographic hint,
explicitly labelled in the pool text. Missing, ambiguous or unavailable matches
remain `stage3_required`, never a guessed DOI. Stage 3's existing target/outcome
ladder handles titles, references and full text (including the first page), multiple
originals, and DOI verification. The harvest inventory is therefore not a fully
linked extraction artifact. Current published FLoRA target metadata takes priority
over an older shard hint; cached hints are restored only for unpublished records.

## Identity, checkpoint and provenance

A DOI-less report uses pool id `i4r:N`, mapped to **negative** engine work id `-N`.
Positive ids remain OpenAlex ids; no synthetic W-id is sent to OpenAlex or written
into `openalex_id_r`. Future curated namespaces must reserve a distinct range.
Exact DOI/handle matches already in the pool reuse its existing identity. The
`curated-i4r-000NNN.parquet` shard sorts before snapshot shards, so routing and tier
readers use its curated metadata once per work. Existing alias resolution still
applies. No fuzzy title merge is performed.

Each shard is published atomically after syncing its file, then syncing the pool
directory; its existence is the incremental checkpoint. Cache and report writes
use the same durable replacement protocol.
Reruns fetch the catalogue but reuse details and skip completed shards. A crash
before publication retries that paper. Requests use four workers by default (`I4R_WORKERS`), with a shared limit of two
requests per second per service and locks around individual response-cache files.
A process lock prevents overlapping harvests
sharing a cache. Keep the same cache and pool configuration for scheduled runs.
This is an append-only new-DP harvester; source revisions to existing DPs require
a deliberate refresh, and changing hints does not reopen settled extraction verdicts.

The optional pool `source` column survives handoff as `i4r`. Existing snapshot
shards need no rewrite. Shards carry curated provenance in their `source` column and parquet footer and enter the
pool fingerprint and pool sync; a snapshot's gate provenance continues to describe
the snapshot portion. A pool containing only curated shards has no snapshot gate
and routes with the existing UNKNOWN-gate warning. Engine version 3 binds the
new identity/routing behavior to routing releases. The pool fingerprint hashes
only the recorded gate and each file's name, byte size and row count. It does
not hash footer metadata, source values, hints or other file contents: a same-size
rewrite can retain a release ID. Curated source/hint provenance is stored in the
parquet, but is not itself a content fingerprint.

## Weekly scheduling and downstream flow

The box runs pipeline jobs through user systemd units. The supplied weekly timer
runs Monday at 06:00 UTC (plus up to 15 minutes jitter), with persistent catch-up.
The user service retries failures every 15 minutes, including failures before
network connectivity is available; it does not depend on a system-only network
target in the user manager:

```bash
mkdir -p ~/.config/systemd/user
cp scripts/systemd/flora-i4r.service scripts/systemd/flora-i4r.timer ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now flora-i4r.timer
systemctl --user list-timers flora-i4r.timer
```

The service targets the box's canonical checkout at
`~/Documents/Coding/flora-extractor`, uses its `.venv` and `.env`, and caps memory
at 2 GB. Adjust the two checkout paths before installing elsewhere. The timer is
supplied for deployment; the PR dry run does not install or start it.

After all intake and rule changes are present, route once and use its release for
all later steps. `PENDING_RUNS.md` consolidates this with the other owed changes:

```bash
.venv/bin/python -m filter.engine route
read -r -p "Release ID printed by route: " REL
.venv/bin/python -m filter.engine diagnose --spec curated-i4r
.venv/bin/python -m filter.engine diagnose --spec reproduction-claim
.venv/bin/python -m filter.engine screen --tier screen_expensive --release "$REL"
```

The last command is a dry run. When the existing sandbox/run checks are satisfied,
the night runner takes that same release argument, under the required memory cap:

```bash
systemd-run --user --unit="flora-night-$REL" -p MemoryMax=8G -p MemorySwapMax=0 "$(pwd)/scripts/night_run.sh" "$REL"
```

`scripts/night_run.sh` screens and extracts. Then use
`.venv/bin/python -m extract.export --release "$REL"`; this remains the only writer
of `data/extracted.csv`. The separate **flora-validation** repository's
`csv_to_db.py` imports the resolved export for human validation. Neither the
harvester nor its scheduled job writes validation tables or production FLoRA records.
