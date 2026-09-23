"""Route would-be new pool rows through today's Stage 2 rule book, in memory only.

Calls `filter.engine.route.route_batch` — a pure function over a pool-schema batch —
with the live spec bundle minus `curated-observatory` (a `doi_in` list of exactly the
Observatory works, which would make the gap population route itself). Nothing is
written to the store, no release is minted, nothing is screened.

Two populations, each rebuilt into `_POOL_SCHEMA` rows with the recovered abstract as
`abstract_text`:
  gap     the 165 cause-F Observatory works (OpenAlex record from fetch_positives'
          cache; text from the abstract store's `epmc:` rows)
  pubmed  the new-to-pool PubMed stem hits of pubmed_probe.py (OpenAlex record from
          its pmid cache — no authorships/primary_location there, so rules reading
          those columns see nulls; PubMed abstract as the text)

    .venv/bin/python -m analysis.recall_pregate.route_offline
"""

import gzip
import json
from dataclasses import replace
from pathlib import Path

import pandas as pd
import pyarrow as pa
from lxml import etree

from filter.engine.route import route_batch
from filter.engine.spec import load_specs
from search.snapshot_scan import _POOL_SCHEMA
from shared import abstract_store
from shared.utils import clean_doi

HERE = Path(__file__).resolve().parent
C = HERE / "cache"
ROOT = HERE.parents[1]


def _row(rec: dict, text: str) -> dict:
    return {
        "id": rec.get("id"), "doi": rec.get("doi"), "title": rec.get("display_name"),
        "display_name": rec.get("display_name"), "publication_year": rec.get("publication_year"),
        "type": rec.get("type"),
        "authorships": json.dumps(rec["authorships"]) if rec.get("authorships") else None,
        "primary_location": json.dumps(rec["primary_location"]) if rec.get("primary_location") else None,
        "open_access": None,
        "concepts": json.dumps(rec.get("concepts") or []),
        "abstract_text": text, "hit_token_title": False, "hit_token_abstract": True,
        "hit_concept": False,
    }


# The two abstract-position admission rules are `shadow: true` in the live bundle, so
# a row whose only claim is in its abstract routes to pending/no_filter_matched today.
# The "promoted" variant flips them live, to show what the intake would be if they were.
ABSTRACT_CLAIMS = {"replication-claim-text", "replication-claim-residual"}


def _route(rows: list[dict], promote: bool = False) -> pd.DataFrame:
    specs = [s for s in load_specs(ROOT / "filter" / "spec") if s.id != "curated-observatory"]
    if promote:
        specs = [replace(s, shadow=False) if s.id in ABSTRACT_CLAIMS else s for s in specs]
    batch = pa.Table.from_pylist(rows, schema=_POOL_SCHEMA).to_batches()[0]
    return route_batch(specs, batch).to_pandas()


def gap_rows() -> list[dict]:
    f = pd.read_csv(C / "F_group.csv")
    want = set(f[(~f.ours) & f.stem].doi)
    rows = []
    for path in (C / "oa_positives").glob("*.json"):
        for rec in json.loads(path.read_text())["results"]:
            doi = clean_doi(rec.get("doi") or "")
            if doi in want:
                found, text = abstract_store.lookup(f"epmc:{doi}")
                rows.append(_row(rec, text or ""))
                want.discard(doi)
    return rows


def pubmed_rows() -> list[dict]:
    new = pd.read_parquet(C / "pubmed_new_hits.parquet")
    new = new[new.oa_id.notna()]
    want = set(new.pmid)
    text = {}
    for f in sorted((C / "pubmed").glob("*.xml.gz")):
        for _, el in etree.iterparse(gzip.open(f), tag="PubmedArticle"):
            pmid = el.findtext("MedlineCitation/PMID")
            if pmid in want:
                art = el.find("MedlineCitation/Article")
                text[pmid] = " ".join("".join(t.itertext()) for t in art.findall("Abstract/AbstractText"))
            el.clear()
    recs = {}
    for path in (C / "oa_pmid").glob("*.json"):
        for rec in json.loads(path.read_text())["results"]:
            recs[str((rec.get("ids") or {}).get("pmid") or "").rsplit("/", 1)[-1]] = rec
    return [_row(recs[p], text.get(p, "")) for p in new.pmid if p in recs]


def main() -> None:
    for (name, rows), promote in [(x, p) for x in (("gap", gap_rows()), ("pubmed", pubmed_rows()))
                                  for p in (False, True)]:
        routed = _route(rows, promote)
        tag = "promoted" if promote else "live"
        routed.to_parquet(C / f"routed_{name}_{tag}.parquet", index=False)
        print(f"\n{name} ({tag} bundle): {len(rows)} rows")
        print(routed.groupby(["pile", "pending_reason"]).size().to_string())
        print(routed[routed.pile == "screen_expensive"].rule_id.value_counts().to_string())


if __name__ == "__main__":
    main()
