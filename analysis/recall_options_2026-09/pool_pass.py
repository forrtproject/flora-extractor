"""One pool pass: full text, a domain proxy and per-ARM masks for every work the
three candidate admission rules match (general / text / residual).

Option membership comes from the masks `analysis/stage2_rules_2026-09/route_scratch.py`
already wrote over release c048ab6483d3's inputs (`evals_proposed.parquet`); this pass
only adds what those masks lack: the whole abstract (the screen reads it), each arm's
own mask (an engine spec per arm, evaluated with the engine backend on the same
overlay-applied batch), and the top level-0 concept as a domain proxy (the pool has no
`primary_topic`).

Read-only: no store, no release, no calls.

    .venv/bin/python -m analysis.recall_options_2026-09.pool_pass
"""

import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pyarrow as pa
import pyarrow.compute as pc
import pyarrow.parquet as pq

from filter.engine.backends import BatchContext
from filter.engine.pool_reader import iter_pool_batches
from filter.engine.route import eval_all
from filter.engine.spec import load_specs
from filter.engine.workids import load_aliases, resolve, work_id
from shared.config import OVERLAY_DIR, SNAPSHOT_POOL_DIR
from shared.utils import clean_doi

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
SCRATCH = ROOT / "cache" / "stage2_rules_2026-09"
OUT = HERE / "out"
ARM_DIR = HERE / "arm_specs"
SPEC_HEAD = HERE / "spec_head" / "filter" / "spec"
G, T, R = "replication-claim-general", "replication-claim-text", "replication-claim-residual"

DOMAIN = {  # OpenAlex level-0 concept -> OpenAlex domain (proxy)
    "Psychology": "Social", "Sociology": "Social", "Economics": "Social",
    "Political science": "Social", "Business": "Social", "Philosophy": "Social",
    "History": "Social", "Art": "Social", "Geography": "Social",
    "Medicine": "Health",
    "Biology": "Life",
    "Physics": "Physical", "Chemistry": "Physical", "Computer science": "Physical",
    "Mathematics": "Physical", "Engineering": "Physical", "Materials science": "Physical",
    "Geology": "Physical", "Environmental science": "Physical",
}


def write_arm_specs() -> None:
    ARM_DIR.mkdir(exist_ok=True)
    srcs = {"g": HERE.parents[0] / "stage2_rules_2026-09/proposed_specs/replication-claim-general.json",
            "t": SPEC_HEAD / "replication-claim-text.json",
            "r": SPEC_HEAD / "replication-claim-residual.json"}
    for tag, path in srcs.items():
        spec = json.loads(path.read_text())
        for i, arm in enumerate(spec["match"]["any_of"], 1):
            (ARM_DIR / f"arm-{tag}{i}.json").write_text(json.dumps({
                "id": f"arm-{tag}{i}", "description": f"arm {i} of {spec['id']}",
                "match": arm, "pile": "screen_expensive", "vocabulary": None,
                "precedence": 100, "shadow": True, "measured": []}, indent=1))


def top_domain(concepts: str) -> str:
    try:
        cs = [c for c in json.loads(concepts or "[]") if c.get("level") == 0]
    except ValueError:
        return ""
    if not cs:
        return ""
    return DOMAIN.get(max(cs, key=lambda c: c.get("score") or 0)["display_name"], "Other")


def main() -> None:
    write_arm_specs()
    arms = load_specs(ARM_DIR)
    df = pd.read_parquet(SCRATCH / "evals_proposed.parquet",
                         columns=["work_id", f"m:{G}", f"m:{T}", f"m:{R}"])
    df = df.drop_duplicates("work_id", keep="first")
    wanted = set(df.work_id[df[f"m:{G}"] | df[f"m:{T}"] | df[f"m:{R}"]])
    print("wanted works:", len(wanted), flush=True)
    aliases = load_aliases(SPEC_HEAD / "aliases.json")
    seen: set[int] = set()
    parts = []
    for n, batch in enumerate(iter_pool_batches(SNAPSHOT_POOL_DIR, OVERLAY_DIR, aliases=aliases)):
        ids = [resolve(work_id(x), aliases) for x in batch.column("id").to_pylist()]
        keep = [i for i, w in enumerate(ids) if w in wanted and w not in seen]
        if not keep:
            continue
        # first occurrence wins, as the routing store does
        uniq, idx = {}, []
        for i in keep:
            if ids[i] not in uniq:
                uniq[ids[i]] = i
                idx.append(i)
        seen.update(uniq)
        sub = batch.take(pa.array(idx))
        ctx = BatchContext(sub)
        masks = eval_all(arms, sub, ctx)
        rec = {
            "work_id": [ids[i] for i in idx],
            "doi": [clean_doi(d or "") for d in sub.column("doi").to_pylist()],
            "title": [(a or b or "") for a, b in zip(sub.column("display_name").to_pylist(),
                                                    sub.column("title").to_pylist())],
            "abstract": [a or "" for a in sub.column("abstract_text").to_pylist()],
            "domain": [top_domain(c) for c in sub.column("concepts").to_pylist()],
            "type": sub.column("type").to_pylist(),
            "year": sub.column("publication_year").to_pylist(),
        }
        for s in arms:
            rec[s.id] = np.asarray(masks[s.id].to_numpy(zero_copy_only=False), dtype=bool)
        parts.append(pd.DataFrame(rec))
        if n % 20 == 0:
            print(n, len(seen), flush=True)
    out = pd.concat(parts, ignore_index=True)
    out.to_parquet(OUT / "cand_text.parquet", index=False)
    print("written", len(out), "of", len(wanted))


if __name__ == "__main__":
    main()
