"""Promotion check for option B-repo: the works `replication-claim-general` (with its
repository exclusion) adds to screen_expensive beyond release 236bc39c2263, measured
offline from `route_scratch.py`'s masks over the CURRENT working-tree bundle and engine.

    .venv/bin/python -m analysis.stage2_rules_2026-09.route_scratch \
        --out cache/stage2_rules_2026-09/evals_brepo.parquet
    .venv/bin/python -m analysis.recall_options_2026-09.measure_brepo

Also checks the reproduction: the masks with the general rule held shadow must give
236bc's piles for every work (the store is read read-only).
"""

import sys
from pathlib import Path

import duckdb
import pandas as pd

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
sys.path.insert(0, str(HERE.parent / "stage2_rules_2026-09"))
from variants import live_set, load, route  # noqa: E402

from filter.engine.workids import work_id  # noqa: E402
from shared.utils import clean_doi  # noqa: E402

BASE = "236bc39c22633076d83cd53f44aece28beb8ed2435afdadcc04902d98bf584ba"
G = "replication-claim-general"


def main() -> None:
    df, meta = load(ROOT / "cache/stage2_rules_2026-09/evals_brepo.parquet")
    live = live_set(meta)
    assert G in live
    pile_new, rule_new = route(df, meta, live)
    pile_old, _ = route(df, meta, live - {G})

    con = duckdb.connect(str(ROOT / "cache/engine/engine.duckdb"), read_only=True)
    store = con.execute("select work_id, pile from routing where release_id = ? "
                        "and pile <> 'pending'", [BASE]).df()
    base = dict(zip(store.work_id, store.pile))
    scratch_old = {w: p for w, p in zip(df.work_id, pile_old) if p != "pending"}
    moved = {w for w in set(base) | set(scratch_old) if base.get(w) != scratch_old.get(w)}
    print(f"236bc non-pending works {len(base):,}; scratch(general shadow) "
          f"{len(scratch_old):,}; differing {len(moved)}")
    for w in sorted(moved)[:20]:
        print(f"   W{w}: store {base.get(w, 'pending')} -> scratch {scratch_old.get(w, 'pending')}")

    new = (pile_new == "screen_expensive") & (pile_old != "screen_expensive")
    other = (pile_new != pile_old) & ~new
    print(f"new screen_expensive works from {G}: {int(new.sum()):,} "
          f"(attributed: {rule_new[new].value_counts().to_dict()}); other moves {int(other.sum())}")
    print(f"screen_expensive: {int((pile_old == 'screen_expensive').sum()):,} -> "
          f"{int((pile_new == 'screen_expensive').sum()):,}")

    flora = pd.read_csv(ROOT / "data/flora.csv", dtype=str)
    flora_d = {clean_doi(u) for u in flora.doi_r.dropna()} - {""}
    obs = pd.read_csv(ROOT / "analysis/mo_observatory/replications_database_2026_09_04_184008.csv",
                      dtype=str, usecols=["replication_url"])
    obs_d = {clean_doi(u) for u in obs.replication_url.dropna()} - {""}
    ex = pd.read_csv(ROOT / "data/extracted.csv", dtype=str, usecols=["openalex_id_r"])
    ex_w = set(ex.openalex_id_r.dropna().map(work_id))
    d = df[new]
    known = (d.doi.isin(flora_d | obs_d) & (d.doi != "")) | d.raw_work_id.isin(ex_w)
    print(f"known replications among new: {int(known.sum())} "
          f"({1000 * known.sum() / max(len(d), 1):.1f}/1k; FLoRA "
          f"{int((d.doi.isin(flora_d) & (d.doi != '')).sum())})")
    print(f"new with no abstract (titled-OSF exemption): {int(d.abstract_empty.sum())}")


if __name__ == "__main__":
    main()
