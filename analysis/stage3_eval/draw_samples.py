"""Draw the two frozen samples the Stage 3 resolution evaluation is measured on.

Run ONCE. The worklist shrinks as works settle, so a sample redrawn later is a
different sample and every earlier number becomes incomparable — which is why the
output is committed and this script is never re-run to "refresh" it.

Writes:
  samples.json  dev + holdout ids, the seed, the release, and each sample's
                identifier composition next to the whole worklist's
  dev_ids.txt   the dev ids alone, comma-joined, so the `--only` of a run command
                cannot accidentally be pasted from a file that holds the holdout
"""

import collections
import json
import random
from pathlib import Path

from extract.tier import extract_works
from filter.engine.claims import ClaimsClient
from filter.engine.overlay import chunk_paths
from filter.engine.release import read_release
from filter.engine.store import DEFAULT_STORE_PATH, open_store, resolve_release
from shared.config import OVERLAY_DIR, SNAPSHOT_POOL_DIR as POOL_DIR

RELEASE = "bc38ddd787e0"
SEED = 20260807
N = 100
HERE = Path(__file__).parent


def kind_of(row: dict) -> str:
    """The identifier composition that decides whether a work can acquire a document
    at all: an OSF registration is a form, a URL-only row has no OpenAlex record."""
    doi = (row.get("doi_r") or "").strip().lower()
    url = (row.get("url_r") or "").strip()
    if doi.startswith("10.17605/osf.io"):
        return "osf_registration"
    if doi:
        return "other_doi"
    return "url_only" if url else "no_identifier"


def main() -> None:
    con = open_store(DEFAULT_STORE_PATH, read_only=True)
    full_release = resolve_release(con, RELEASE)
    record = read_release(full_release, cache_dir=DEFAULT_STORE_PATH.parent) or {}
    overlay = OVERLAY_DIR if chunk_paths(OVERLAY_DIR) else None
    works = extract_works(con, ClaimsClient(), POOL_DIR, full_release,
                          record=record, overlay_dir=overlay)

    kinds = {w.work_id: kind_of(w.row) for w in works}
    ids = sorted(kinds)                      # deterministic order before the shuffle
    random.Random(SEED).shuffle(ids)
    dev, holdout = ids[:N], ids[N:2 * N]
    assert not set(dev) & set(holdout)

    def composition(subset: list[str]) -> dict:
        counted = collections.Counter(kinds[i] for i in subset)
        return {k: counted.get(k, 0) for k in
                ("other_doi", "osf_registration", "url_only", "no_identifier")}

    out = {
        "release": full_release, "seed": SEED, "n_worklist": len(works),
        "composition": {"worklist": composition(ids), "dev": composition(dev),
                        "holdout": composition(holdout)},
        "dev": dev, "holdout": holdout,
    }
    (HERE / "samples.json").write_text(json.dumps(out, indent=2))
    (HERE / "dev_ids.txt").write_text(",".join(str(i) for i in dev))
    print(json.dumps({k: out[k] for k in
                      ("release", "seed", "n_worklist", "composition")}, indent=2))


if __name__ == "__main__":
    main()
