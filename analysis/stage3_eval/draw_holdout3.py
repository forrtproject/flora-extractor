"""Draw the THIRD holdout, for the CrossRef-first resolver of iterations 13-15.

Holdout 2 judged the pooled resolver. Iterations 13-15 came after it, and two of them
were designed off evidence in holdout 1's misses — so neither earlier holdout can judge
them. This one is drawn from the works used in none of the three samples.

Run ONCE. Same release, same worklist build, a different seed recorded here.
"""

import collections
import json
import random
from pathlib import Path

from analysis.stage3_eval.draw_samples import RELEASE, kind_of
from extract.tier import extract_works
from filter.engine.claims import ClaimsClient
from filter.engine.overlay import chunk_paths
from filter.engine.release import read_release
from filter.engine.store import DEFAULT_STORE_PATH, open_store, resolve_release
from shared.config import OVERLAY_DIR, SNAPSHOT_POOL_DIR as POOL_DIR

SEED = 20260809
N = 100
HERE = Path(__file__).parent


def main() -> None:
    con = open_store(DEFAULT_STORE_PATH, read_only=True)
    full_release = resolve_release(con, RELEASE)
    record = read_release(full_release, cache_dir=DEFAULT_STORE_PATH.parent) or {}
    overlay = OVERLAY_DIR if chunk_paths(OVERLAY_DIR) else None
    works = extract_works(con, ClaimsClient(), POOL_DIR, full_release,
                          record=record, overlay_dir=overlay)

    first = json.loads((HERE / "samples.json").read_text())
    second = json.loads((HERE / "samples-holdout2.json").read_text())
    spent = set(first["dev"]) | set(first["holdout"]) | set(second["holdout2"])
    kinds = {w.work_id: kind_of(w.row) for w in works if w.work_id not in spent}

    # The worklist has moved since the first draw — works settle and leave it — so it
    # is rebuilt rather than remembered, and only the two spent samples are subtracted.
    ids = sorted(kinds)
    random.Random(SEED).shuffle(ids)
    drawn = ids[:N]

    def composition(subset):
        counted = collections.Counter(kinds[i] for i in subset)
        return {k: counted.get(k, 0) for k in
                ("other_doi", "osf_registration", "url_only", "no_identifier")}

    out = {"release": full_release, "seed": SEED, "n_unspent": len(ids),
           "excluded": {"dev": len(first["dev"]), "holdout": len(first["holdout"]),
                        "holdout2": len(second["holdout2"])},
           "composition": {"unspent": composition(ids), "holdout3": composition(drawn)},
           "holdout3": drawn}
    (HERE / "samples-holdout3.json").write_text(json.dumps(out, indent=2))
    print(json.dumps({k: out[k] for k in
                      ("release", "seed", "n_unspent", "excluded", "composition")},
                     indent=2))


if __name__ == "__main__":
    main()
