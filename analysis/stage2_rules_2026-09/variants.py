"""Re-derive a release's piles from the masks `route_scratch.py` wrote, under variants.

Routing is a pure function of the per-spec masks: the first non-shadow spec by
(-precedence, id) wins, and a screening pile is downgraded to `pending/no_text`
when the abstract is empty and the row is not a titled OSF record. So "what if
`curated-observatory` were shadow" or "what if candidate X were live" is a
dataframe operation, not a re-route.

`route(df, meta, live)` returns (pile, rule) per row for the set of live spec ids.
Rows are deduplicated on the alias-resolved work id, first writer wins, exactly
as `store._insert_routing` does (the pass preserves pool order).
"""

import json
from pathlib import Path

import numpy as np
import pandas as pd

TEXT_PILES = {"screen_expensive", "screen_cheap"}
ADMITTED = {"screen_expensive", "screen_cheap", "needs_human"}


def load(path: Path) -> "tuple[pd.DataFrame, dict]":
    df = pd.read_parquet(path)
    meta = json.loads(path.with_suffix(".specs.json").read_text())
    df = df.drop_duplicates("work_id", keep="first").reset_index(drop=True)
    return df, meta


def route(df: pd.DataFrame, meta: dict, live: set) -> "tuple[pd.Series, pd.Series]":
    order = sorted(live, key=lambda s: (-meta["precedence"][s], s))
    pile = np.full(len(df), "pending", dtype=object)
    rule = np.full(len(df), "", dtype=object)
    open_ = np.ones(len(df), dtype=bool)
    downgrade = (df["abstract_empty"] & ~df["screenable_title"]).to_numpy()
    for sid in order:
        hit = df["m:" + sid].to_numpy() & open_
        p = meta["pile"][sid]
        rule[hit] = sid
        if p in TEXT_PILES:
            pile[hit & ~downgrade] = p
            pile[hit & downgrade] = "pending"
        else:
            pile[hit] = p
        open_ &= ~hit
    return pd.Series(pile, index=df.index), pd.Series(rule, index=df.index)


def live_set(meta: dict) -> set:
    return {s for s in meta["live"] if not meta["shadow"][s]}
