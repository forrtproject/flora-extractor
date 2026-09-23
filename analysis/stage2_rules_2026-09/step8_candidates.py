"""Step 8: reach of each candidate arm with curated-observatory shadow."""
from pathlib import Path
import pandas as pd
from variants import load, route, live_set
from shared.utils import clean_doi

ROOT = Path("/home/lukas/flora-extractor")
df, meta = load(ROOT / "cache/stage2_rules_2026-09/evals_cand1.parquet")
obs = pd.read_csv(ROOT / "analysis/mo_observatory/replications_database_2026_09_04_184008.csv",
                  dtype=str, usecols=["replication_url"])
obs_dois = {clean_doi(u) for u in obs.replication_url.dropna()}
flora = pd.read_csv(ROOT / "data/flora.csv", dtype=str)
flora_dois = {clean_doi(u) for u in flora.doi_r.dropna()}
df["obs"] = df.doi.isin(obs_dois) & (df.doi != "")
df["flora"] = df.doi.isin(flora_dois) & (df.doi != "")
live = live_set(meta)
base = live - {"curated-observatory"}
now, _ = route(df, meta, live)
sh, _ = route(df, meta, base)
today = now == "screen_expensive"
lost = today & (sh != "screen_expensive") & df["m:curated-observatory"]
print("today", today.sum(), "after shadow", (sh == "screen_expensive").sum(), "lost curated", lost.sum())
print("gold in pool: obs", df.obs.sum(), "flora", df.flora.sum())
cands = [s for s in meta["order"] if s.startswith("cand-")]
out = []
for c in cands:
    p, _ = route(df, meta, base | {c})
    adm = p == "screen_expensive"
    new = adm & ~today
    out.append({"cand": c, "matches": int(df["m:" + c].sum()),
                "lost_recovered": int((adm & lost).sum()),
                "new_beyond_today": int(new.sum()),
                "new_obs": int((new & df.obs).sum()), "new_flora": int((new & df.flora).sum()),
                "new_obs_or_flora": int((new & (df.obs | df.flora)).sum())})
res = pd.DataFrame(out)
res["yield_per_1k"] = (1000 * res.new_obs_or_flora / res.new_beyond_today.clip(lower=1)).round(1)
print(res.to_string())
res.to_csv(ROOT / "analysis/stage2_rules_2026-09/step8_candidates.csv", index=False)
