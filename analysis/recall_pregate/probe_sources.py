"""Ask Europe PMC (and optionally CrossRef) for abstracts of a DOI list — measurement only.

Uses the shipped fetcher `search.fetch_abstracts._fetch_epmc_batch` (100 DOIs per
searchPOST, keyless), but caches into cache/probe_epmc.json here rather than into the
shared abstract store, so the experiment writes nothing the pipeline reads.

    .venv/bin/python -m analysis.recall_pregate.probe_sources --which flora_noabs
    .venv/bin/python -m analysis.recall_pregate.probe_sources --which sample_noabs --n 3000
"""

import argparse
import json
import re
import time
from pathlib import Path

import pandas as pd

from filter.phrase_detection import REPLICATION_STEM_PATTERN
from search.fetch_abstracts import _fetch_crossref_abstract, _fetch_epmc_batch

HERE = Path(__file__).resolve().parent
EPMC_CACHE = HERE / "cache" / "probe_epmc.json"
XREF_CACHE = HERE / "cache" / "probe_crossref.json"
STEM = re.compile(REPLICATION_STEM_PATTERN)


def _load(path: Path) -> dict:
    return json.loads(path.read_text()) if path.exists() else {}


def epmc(dois: list[str]) -> dict:
    cache = _load(EPMC_CACHE)
    todo = [d for d in dict.fromkeys(dois) if d not in cache]
    for i in range(0, len(todo), 100):
        chunk = todo[i:i + 100]
        for attempt in range(5):
            got = _fetch_epmc_batch(chunk)
            if got is not None:
                break
            time.sleep(10 * (attempt + 1))
        if got is None:
            print("EPMC batch failed; stopping (rerun resumes)")
            break
        cache.update(got)
        EPMC_CACHE.write_text(json.dumps(cache))
        time.sleep(0.5)
    return cache


def crossref(dois: list[str]) -> dict:
    cache = _load(XREF_CACHE)
    for d in dict.fromkeys(dois):
        if d in cache:
            continue
        text, status = _fetch_crossref_abstract(d)
        if status == "transient":
            continue
        cache[d] = text
        time.sleep(0.1)
    XREF_CACHE.write_text(json.dumps(cache))
    return cache


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--which", choices=["flora_noabs", "sample_noabs"], required=True)
    ap.add_argument("--n", type=int, default=3000)
    ap.add_argument("--crossref-n", type=int, default=0)
    args = ap.parse_args()
    if args.which == "flora_noabs":
        p = pd.read_parquet(HERE / "cache" / "positives.parquet")
        df = p[(p.set.isin(["flora", "extracted"])) & ~p.has_abstract].drop_duplicates("doi")
    else:
        s = pd.read_parquet(HERE / "cache" / "sample")
        # Stratified on has_pmid: the two strata have very different hit rates, and
        # the PMID one is ~5% of the universe, so a plain sample would barely see it.
        # Probed inside the metadata-narrowed universe (every rule in analyse.RULES but
        # the last, has_pmid), because that is the population a fetch would be spent on.
        from analysis.recall_pregate.analyse import RULES
        pool = s[~s.has_abstract & ~(s.hit_title | s.hit_concept)]
        for _, pred in RULES[:-1]:
            pool = pool[pred(pool)]
        df = pd.concat([g.sample(min(args.n, len(g)), random_state=1)
                        for _, g in pool.groupby("has_pmid")])
    df = df[df.doi.notna()]
    got = epmc(df.doi.tolist())
    df = df.assign(epmc_text=df.doi.map(lambda d: got.get(d)))
    df["epmc_hit"] = df.epmc_text.notna()
    df["epmc_stem"] = df.epmc_text.fillna("").map(lambda t: bool(STEM.search(t)))
    if args.crossref_n:
        sub = df.sample(min(args.crossref_n, len(df)), random_state=2)
        xr = crossref(sub.doi.tolist())
        df["xref_hit"] = df.doi.map(lambda d: bool(xr.get(d)) if d in xr else None)
    out = HERE / "cache" / f"probe_{args.which}.parquet"
    df.drop(columns=["epmc_text"]).to_parquet(out, index=False)
    print(df.groupby("has_pmid")[["epmc_hit", "epmc_stem"]].agg(["mean", "sum", "count"]))


if __name__ == "__main__":
    main()
