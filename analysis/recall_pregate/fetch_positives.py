"""Fetch the OpenAlex records of the positive sets and reduce them to sample features.

Positive sets (DOIs of REPLICATION papers):
  obs_F     the 417 Observatory works the gate missed with no OpenAlex abstract
            (cause F, `analysis/mo_observatory/not_in_pool_causes.csv`); the recall
            target proper is the subset whose recovered abstract fires the gate stem.
  flora     published FLoRA replications (`data/flora.csv` doi_r)
  extracted the pipeline's own Stage 3 output (`data/extracted.csv` doi_r)

50-DOI `filter=doi:` batches (1x filter query each, ~110 requests), cached as raw JSON
under cache/oa_positives/. Features match `sample_snapshot._reduce` so the two frames
can be scored by the same predicates.

    .venv/bin/python -m analysis.recall_pregate.fetch_positives
"""

import hashlib
import json
import re
import time
from pathlib import Path

import pandas as pd
import requests

from filter.phrase_detection import CONCEPT_IDS, REPLICATION_STEM_PATTERN
from shared.openalex_keys import headers
from shared.utils import clean_doi

HERE = Path(__file__).resolve().parent
CACHE = HERE / "cache" / "oa_positives"
ROOT = HERE.parents[1]
_DOI_PREFIX = re.compile(r"(10\.\d{4,9})/")
_STEM = re.compile(REPLICATION_STEM_PATTERN)
_CONCEPTS = {f"https://openalex.org/{c}" for c in CONCEPT_IDS}
SELECT = ("id,doi,display_name,ids,indexed_in,language,type,publication_year,"
          "primary_topic,is_paratext,is_retracted,referenced_works_count,"
          "abstract_inverted_index,primary_location,concepts,cited_by_count,has_content")


def fetch(dois: list[str]) -> list[dict]:
    CACHE.mkdir(parents=True, exist_ok=True)
    out = []
    for i in range(0, len(dois), 50):
        chunk = dois[i:i + 50]
        key = hashlib.sha1("|".join(chunk).encode()).hexdigest()[:16]
        path = CACHE / f"{key}.json"
        if not path.exists():
            for attempt in range(4):
                r = requests.get("https://api.openalex.org/works", headers=headers(), params={
                    "filter": "doi:" + "|".join(chunk), "per-page": 200, "select": SELECT},
                    timeout=60)
                if r.status_code == 200:
                    break
                time.sleep(2 ** attempt)
            r.raise_for_status()
            path.write_text(r.text)
            time.sleep(0.2)
        out.extend(json.loads(path.read_text())["results"])
    return out


def reduce(rec: dict) -> dict:
    ids = rec.get("ids") or {}
    pt = rec.get("primary_topic") or {}
    src = ((rec.get("primary_location") or {}).get("source")) or {}
    doi = clean_doi(rec.get("doi") or "")
    m = _DOI_PREFIX.match(doi)
    aii = rec.get("abstract_inverted_index")
    aj = json.dumps(aii) if aii else ""
    hc = rec.get("has_content") or {}
    return {
        "id": rec.get("id"), "doi": doi or None, "doi_prefix": m.group(1) if m else None,
        "title": rec.get("display_name"), "language": rec.get("language"),
        "type": rec.get("type"), "year": rec.get("publication_year"),
        "has_pmid": bool(ids.get("pmid")), "has_pmcid": bool(ids.get("pmcid")),
        "has_mag": bool(ids.get("mag")),
        "indexed_in": "|".join(sorted(rec.get("indexed_in") or [])),
        "domain": (pt.get("domain") or {}).get("display_name"),
        "field": (pt.get("field") or {}).get("display_name"),
        "subfield": (pt.get("subfield") or {}).get("display_name"),
        "topic": pt.get("display_name"),
        "source_type": src.get("type"), "source_name": src.get("display_name"),
        "host_org": src.get("host_organization_name"), "is_core": src.get("is_core"),
        "is_paratext": rec.get("is_paratext"), "is_retracted": rec.get("is_retracted"),
        "refs": rec.get("referenced_works_count") or 0,
        "cited_by": rec.get("cited_by_count") or 0,
        "has_pdf": bool(hc.get("pdf")), "has_grobid": bool(hc.get("grobid_xml")),
        "has_abstract": bool(aii), "abstract_len": len(aj),
        "hit_title": bool(_STEM.search(rec.get("display_name") or "")),
        "hit_abstract": bool(_STEM.search(aj)),
        "hit_concept": any(c.get("id") in _CONCEPTS for c in (rec.get("concepts") or [])),
    }


def main() -> None:
    sets = {}
    f = pd.read_csv(HERE / "cache" / "F_group.csv")
    sets["obs_F"] = f.doi.tolist()
    fl = pd.read_csv(ROOT / "data" / "flora.csv", usecols=["doi_r"])
    sets["flora"] = fl.doi_r.dropna().map(clean_doi).tolist()
    ex = pd.read_csv(ROOT / "data" / "extracted.csv", usecols=["doi_r"], low_memory=False)
    sets["extracted"] = ex.doi_r.dropna().map(clean_doi).tolist()
    frames = []
    for name, dois in sets.items():
        dois = sorted({d for d in dois if d.startswith("10.")})
        recs = {clean_doi(r.get("doi") or ""): reduce(r) for r in fetch(dois)}
        df = pd.DataFrame([dict(recs[d], set=name, query_doi=d) for d in dois if d in recs])
        print(name, "asked", len(dois), "found", len(df))
        frames.append(df)
    pd.concat(frames).to_parquet(HERE / "cache" / "positives.parquet", index=False)


if __name__ == "__main__":
    main()
