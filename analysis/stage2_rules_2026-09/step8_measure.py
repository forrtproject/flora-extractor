"""Step 8: curated-observatory as a shadow monitor, and the general rule that replaces it.

Reads the masks `route_scratch.py` wrote with `proposed_specs/` evaluated alongside
the live bundle (engine backend, overlay and aliases as `route` would use), and
answers, offline and without a release:

1. With `curated-observatory` shadow, how many of its works still reach
   `screen_expensive`, and through which rules.
2. What the lost works are, and whether the screen and Stage 3 already took them.
3. What `replication-claim-general` recovers, and what it adds beyond today's pile.

Writes `curated_lost.csv` (one row per lost work) and prints the numbers REPORT.md
quotes.

    PYTHONPATH=. .venv/bin/python analysis/stage2_rules_2026-09/step8_measure.py
"""

import sys
from pathlib import Path

import pandas as pd

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from variants import live_set, load, route  # noqa: E402

from filter.engine.workids import work_id  # noqa: E402
from shared.utils import clean_doi  # noqa: E402

ROOT = HERE.parents[1]
SCRATCH = ROOT / "cache" / "stage2_rules_2026-09"
CUR, GEN, TWIN = "curated-observatory", "replication-claim-general", "doi-registry-twin"


def main() -> None:
    df, meta = load(SCRATCH / "evals_proposed.parquet")
    obs = pd.read_csv(ROOT / "analysis/mo_observatory/replications_database_2026_09_04_184008.csv",
                      dtype=str, usecols=["replication_url"])
    gold = ({clean_doi(u) for u in obs.replication_url.dropna()}
            | {clean_doi(u) for u in pd.read_csv(ROOT / "data/flora.csv", dtype=str).doi_r.dropna()})
    df["gold"] = df.doi.isin(gold) & (df.doi != "")
    sd = pd.read_parquet(SCRATCH / "screen_decisions.parquet")
    df["screen"] = df.work_id.map(dict(zip(sd.work_id, sd.outcome))).fillna("")
    ex = pd.read_csv(ROOT / "data/extracted.csv", dtype=str, usecols=["openalex_id_r"])
    df["extracted"] = df.raw_work_id.isin(set(ex.openalex_id_r.map(work_id)))

    live = live_set(meta)
    variants = {
        "today": live,
        "curated_shadow": live - {CUR},
        "curated_shadow+general": (live - {CUR}) | {GEN},
        "general_only_added": live | {GEN},
    }
    piles = {k: route(df, meta, v) for k, v in variants.items()}
    adm = {k: p == "screen_expensive" for k, (p, _r) in piles.items()}
    today = adm["today"]
    print("screen_expensive works:", {k: int(v.sum()) for k, v in adm.items()})

    cur = df[f"m:{CUR}"]
    print(f"\n{CUR}: matches {int(cur.sum())} works; admitted today {int((cur & today).sum())}; "
          f"won by it {int(((piles['today'][1] == CUR) & today).sum())}")
    sh_pile, sh_rule = piles["curated_shadow"]
    keep = cur & today & adm["curated_shadow"]
    lost = cur & today & ~adm["curated_shadow"]
    print(f"with it shadow: still admitted {int(keep.sum())}, lost {int(lost.sum())}")
    print(sh_rule[keep].value_counts().to_string())
    print(f"lost: screen proceed {int((lost & (df.screen == 'proceed')).sum())}, "
          f"discard {int((lost & (df.screen == 'discard')).sum())}, "
          f"in extracted.csv {int((lost & df.extracted).sum())}, "
          f"empty abstract {int((lost & df.abstract_empty).sum())}")
    print("lost matched by shadow rules:", {s: int((lost & df['m:' + s]).sum())
                                            for s in meta['order'] if meta['shadow'].get(s)
                                            and s not in (CUR, TWIN)})

    g = adm["curated_shadow+general"]
    new = g & ~today
    print(f"\n{GEN} (with curated shadow): matches {int(df['m:' + GEN].sum())}; "
          f"recovers {int((g & lost).sum())} of the {int(lost.sum())} lost "
          f"({int((g & lost & (df.screen == 'proceed')).sum())} screen-proceed, "
          f"{int((g & lost & df.extracted).sum())} extracted); "
          f"new beyond today's pile {int(new.sum())}, of them known replications "
          f"{int((new & df.gold).sum())} ({1000 * (new & df.gold).sum() / max(new.sum(), 1):.1f}/1k)")
    print(f"export loss if curated went shadow with {GEN} live: "
          f"{int((lost & df.extracted & ~g).sum())} extracted works leave the admitted piles "
          f"(without {GEN}: {int((lost & df.extracted).sum())})")
    print(f"doi-registry-twin: matches {int(df['m:' + TWIN].sum())}, of them admitted today "
          f"{int((df['m:' + TWIN] & today).sum())}, admitted with curated shadow "
          f"{int((df['m:' + TWIN] & adm['curated_shadow']).sum())}")

    out = df[lost].copy()
    out["recovered_by_general"] = g[lost]
    for s in ("replication-claim-text", "replication-claim-residual", "replication-signal"):
        out["m:" + s] = df.loc[lost, "m:" + s]
    cols = ["work_id", "doi", "year", "type", "source", "title", "abstract_empty", "screen",
            "extracted", "recovered_by_general", "m:replication-claim-text",
            "m:replication-claim-residual", "m:replication-signal"]
    out[cols].rename(columns=lambda c: c.replace("m:", "matches_")).to_csv(
        HERE / "curated_lost.csv", index=False, encoding="utf-8-sig")


if __name__ == "__main__":
    main()
