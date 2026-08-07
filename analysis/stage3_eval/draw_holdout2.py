"""Draw the SECOND holdout, for the pooled-candidate resolver.

The first holdout measured the code as it stood at commit `4b53cb2`. Everything after
that — the pooled candidate list and the fixes from its reviews — is a change the first
holdout cannot judge: it has been read, and a sample you have read is a second
development sample. `docs/stage3-quality-handover.md` §1.4 says so, and says where the
next one comes from: the works used in NEITHER existing sample.

Run ONCE, like `draw_samples.py`. Same release, same worklist build, same seed
discipline — a different seed, recorded here, over the 1,098 works left.
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

SEED = 20260808
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
    spent = set(first["dev"]) | set(first["holdout"])
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
           "excluded": {"dev": len(first["dev"]), "holdout": len(first["holdout"])},
           "composition": {"unspent": composition(ids), "holdout2": composition(drawn)},
           "holdout2": drawn}
    (HERE / "samples-holdout2.json").write_text(json.dumps(out, indent=2))
    print(json.dumps({k: out[k] for k in
                      ("release", "seed", "n_unspent", "excluded", "composition")},
                     indent=2))


if __name__ == "__main__":
    main()
