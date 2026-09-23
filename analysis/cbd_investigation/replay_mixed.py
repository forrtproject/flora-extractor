"""Step 5: replay mixed rows under HEAD rules (M0) and the narrowed mixed definition (M1).

Rows: the 40 pairs where we say mixed and the Observatory says only success (24) or
only failure (16), plus 30 other mixed rows drawn at random (seed 7) to size the policy
effect. Same evidence and model as replay.py; answers share its cache.
"""
import json
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import replay as R  # noqa: E402  (sets env, cwd, path)
import pandas as pd  # noqa: E402
import variants as V  # noqa: E402

HERE = Path(__file__).parent
d = pd.read_csv("data/extracted.csv", dtype=str, keep_default_na=False)
c = pd.read_csv(HERE / "out" / "all_rows_cache.csv", dtype=str, keep_default_na=False)
x = d[d.type == "replication"].merge(c[["pair_id", "pre_file", "ft_file"]], on="pair_id").drop_duplicates("pair_id")
pairs = pd.read_csv(HERE / "out" / "mixed_pairs.csv", dtype=str, keep_default_na=False)
a = x.merge(pairs[["doi_r", "doi_o", "mo"]], on=["doi_r", "doi_o"])
a = a.assign(group="mixed_vs_mo_" + a.mo)
rest = x[(x.outcome == "mixed") & ~x.pair_id.isin(a.pair_id) & ((x.ft_file != "") | (x.pre_file != ""))]
s = pd.concat([a, rest.sample(30, random_state=7).assign(group="mixed_random")])


def run(row):
    src = row.ft_file or row.pre_file
    if not src:
        return []
    ev = R.evidence_of(json.load(open(src))["llm_prompt"])
    rtc = "DISCUSSION / CONCLUSION (from" in ev or V.FULL_BODY_HEADER[:20] in ev
    head = V.head_prefix(rtc)
    out = []
    for name, p in {"M0": head + ev, "M1": V.apply(head, V.MIXED_EDITS) + ev}.items():
        rec = R.call(p)
        t = R.match_target(rec.get("result"), row, ev)
        out.append({"pair_id": row.pair_id, "group": row.group, "variant": name,
                    "shipped": row.outcome, "outcome": (t or {}).get("outcome", ""),
                    "reason": (t or {}).get("outcome_reasoning", ""), "err": rec.get("err", "")})
    return out


if __name__ == "__main__":
    print(s.group.value_counts())
    with ThreadPoolExecutor(12) as ex:
        res = [r for rows in ex.map(lambda t: run(t[1]), s.iterrows()) for r in rows]
    df = pd.DataFrame(res)
    df.to_csv(HERE / "out" / "replay_mixed.csv", index=False)
    print(df.pivot_table(index=["group", "outcome"], columns="variant", values="pair_id", aggfunc="count", fill_value=0))
