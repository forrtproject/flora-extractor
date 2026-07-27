#!/usr/bin/env bash
#
# Sync the large Stage 1/2 data files (candidates.csv ~4.7GB, filtered.csv ~4.3GB).
#
# These are too big for git / GitHub LFS free tier, so they are stored — zipped —
# in a Cloudflare R2 bucket and versioned with DVC. Only the small *.dvc pointer
# files are committed to git. The unzipped CSVs are the working copies the pipeline
# reads; they are gitignored and regenerated from the zips.
#
#   ./scripts/data.sh pull        # dvc pull the zips from R2, then unzip to CSVs
#   ./scripts/data.sh pack        # re-zip updated CSVs, dvc add (then commit + push)
#   ./scripts/data.sh push        # dvc push (R2-compatibility env vars set — see below)
#   ./scripts/data.sh prune <N>   # keep only the last N versions in cache + R2 (dry-run;
#                                 # append 'apply' to actually delete)
#
# Requires DVC with R2 credentials configured in .dvc/config.local (see docs/setup.md).
set -euo pipefail
cd "$(dirname "$0")/.."

# botocore/aiobotocore >=1.36-ish default to a checksum-calculation mode that adds
# trailing checksums via chunked transfer encoding on multipart uploads. R2 (and other
# non-AWS S3-compatible endpoints) doesn't handle that the same way AWS does, so a
# multi-GB push fails with "You did not provide the number of bytes specified by the
# Content-Length HTTP header" (dvc.exceptions.UploadError). Confirmed 2026-07-26 on
# this repo's candidates.zip/filtered.zip push. Forcing "when_required" restores the
# older, R2-compatible behavior.
export AWS_REQUEST_CHECKSUM_CALCULATION=when_required
export AWS_RESPONSE_CHECKSUM_VALIDATION=when_required

case "${1:-pull}" in
  pull)
    dvc pull data/candidates.zip.dvc data/filtered.zip.dvc
    # python's zipfile is used instead of unzip/zip so no external archive tool
    # is required (Git Bash on Windows ships neither reliably).
    ( cd data && python -m zipfile -e candidates.zip . && python -m zipfile -e filtered.zip . )
    echo "✓ data/candidates.csv and data/filtered.csv ready"
    ;;
  pack)
    ( cd data && rm -f candidates.zip filtered.zip \
        && python -m zipfile -c candidates.zip candidates.csv \
        && python -m zipfile -c filtered.zip filtered.csv )
    dvc add data/candidates.zip data/filtered.zip
    echo "✓ re-zipped and dvc-added. Next:"
    echo "    git add data/candidates.zip.dvc data/filtered.zip.dvc"
    echo "    git commit -m 'Update Stage 1/2 data'"
    echo "    ./scripts/data.sh push"
    ;;
  push)
    dvc push
    ;;
  prune)
    # Keep the last N committed versions (counted in git commits back from HEAD, plus
    # the current workspace), deleting older blobs from both the local cache and R2.
    # NOTE: --num counts git commits, not data updates. If code commits sit between
    # data updates you may keep fewer than N *data* versions; tag snapshots and use
    # `dvc gc --all-tags` if you need exact per-version control.
    keep="${2:?usage: $0 prune <N> [apply]}"
    if [ "${3:-}" = "apply" ]; then
      dvc gc --workspace --rev HEAD --num "$keep" --cloud -f
    else
      echo "DRY RUN — would remove the following (append 'apply' to execute):"
      dvc gc --workspace --rev HEAD --num "$keep" --cloud --dry
    fi
    ;;
  *)
    echo "usage: $0 [pull|pack|push|prune <N> [apply]]" >&2
    exit 1
    ;;
esac
