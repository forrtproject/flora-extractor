"""Run the search gate over a sample of the PubMed baseline and see what the pool misses.

The inversion being tested: rather than fetching an abstract for every OpenAlex work
that lacks one (tens of millions) and gating it, gate the external abstract corpus
itself — PubMed ships as a free bulk baseline — and pull only its stem hits into the
pool by identifier.

For K baseline files spread across the PMID range: parse every citation (PMID, DOI,
year, title, abstract), apply REPLICATION_STEM_PATTERN to the title and to the abstract
text (a plain-text match is a superset of the inverted-index match the snapshot gate
does: same stems, same case-insensitivity), then check each stem hit against
  * the survivor pool, by DOI (cache/pool_dois.parquet, built once from the pool), and
  * OpenAlex, by `filter=pmid:` batches of 50 (1x filter query each): does the work
    exist, does it carry an abstract there, would the snapshot gate have passed it.

    .venv/bin/python -m analysis.recall_pregate.pubmed_probe --files 100,400,700,1000,1300
"""

import argparse
import gzip
import hashlib
import json
import re
import time
from pathlib import Path

import pandas as pd
import pyarrow.parquet as pq
import requests
from lxml import etree

from filter.phrase_detection import CONCEPT_IDS, REPLICATION_STEM_PATTERN
from shared.config import SNAPSHOT_POOL_DIR
from shared.openalex_keys import headers
from shared.utils import clean_doi

HERE = Path(__file__).resolve().parent
PM = HERE / "cache" / "pubmed"
BASE = "https://ftp.ncbi.nlm.nih.gov/pubmed/baseline/pubmed26n{:04d}.xml.gz"
STEM = re.compile(REPLICATION_STEM_PATTERN)
_CONCEPTS = {f"https://openalex.org/{c}" for c in CONCEPT_IDS}


def download(n: int) -> Path:
    PM.mkdir(parents=True, exist_ok=True)
    path = PM / f"pubmed26n{n:04d}.xml.gz"
    if not path.exists():
        with requests.get(BASE.format(n), stream=True, timeout=300) as r:
            r.raise_for_status()
            tmp = path.with_suffix(".part")
            with open(tmp, "wb") as f:
                for chunk in r.iter_content(1 << 20):
                    f.write(chunk)
            tmp.rename(path)
    return path


def parse(path: Path) -> pd.DataFrame:
    rows = []
    for _, el in etree.iterparse(gzip.open(path), tag="PubmedArticle"):
        cit = el.find("MedlineCitation")
        pmid = cit.findtext("PMID")
        art = cit.find("Article")
        title = "".join(art.find("ArticleTitle").itertext()) if art.find("ArticleTitle") is not None else ""
        abstract = " ".join("".join(t.itertext()) for t in art.findall("Abstract/AbstractText"))
        doi = None
        for aid in el.findall("PubmedData/ArticleIdList/ArticleId"):
            if aid.get("IdType") == "doi" and aid.text:
                doi = clean_doi(aid.text)
        year = art.findtext("Journal/JournalIssue/PubDate/Year")
        ptypes = "|".join(p.text or "" for p in art.findall("PublicationTypeList/PublicationType"))
        rows.append({"pmid": pmid, "doi": doi, "year": int(year) if year and year.isdigit() else None,
                     "lang": art.findtext("Language"), "ptypes": ptypes,
                     "has_abstract": bool(abstract.strip()), "abstract_chars": len(abstract),
                     "stem_title": bool(STEM.search(title)), "stem_abstract": bool(STEM.search(abstract)),
                     "title": title, "file": path.name})
        el.clear()
    return pd.DataFrame(rows)


def pool_dois() -> set[str]:
    path = HERE / "cache" / "pool_dois.parquet"
    if not path.exists():
        dois = []
        for f in sorted(SNAPSHOT_POOL_DIR.glob("*.parquet")):
            dois.extend(pq.read_table(f, columns=["doi"]).column("doi").to_pylist())
        pd.DataFrame({"doi": sorted({clean_doi(d) for d in dois if d})}).to_parquet(path)
    return set(pd.read_parquet(path).doi)


def openalex_by_pmid(pmids: list[str]) -> dict:
    cache_dir = HERE / "cache" / "oa_pmid"
    cache_dir.mkdir(parents=True, exist_ok=True)
    out = {}
    for i in range(0, len(pmids), 50):
        chunk = pmids[i:i + 50]
        path = cache_dir / (hashlib.sha1("|".join(chunk).encode()).hexdigest()[:16] + ".json")
        if not path.exists():
            for attempt in range(4):
                r = requests.get("https://api.openalex.org/works", headers=headers(), timeout=60,
                                 params={"filter": "pmid:" + "|".join(chunk), "per-page": 200,
                                         "select": "id,doi,ids,type,language,publication_year,"
                                                   "display_name,abstract_inverted_index,concepts"})
                if r.status_code == 200:
                    break
                time.sleep(2 ** attempt)
            r.raise_for_status()
            path.write_text(r.text)
            time.sleep(0.15)
        for rec in json.loads(path.read_text())["results"]:
            pm = str((rec.get("ids") or {}).get("pmid") or "").rsplit("/", 1)[-1]
            aii = rec.get("abstract_inverted_index")
            out[pm] = {"oa_id": rec["id"], "oa_doi": clean_doi(rec.get("doi") or ""),
                       "oa_type": rec.get("type"), "oa_has_abstract": bool(aii),
                       "oa_gate": bool(STEM.search(rec.get("display_name") or "")
                                       or (aii and STEM.search(json.dumps(aii)))
                                       or any(c.get("id") in _CONCEPTS for c in rec.get("concepts") or []))}
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--files", default="100,400,700,1000,1300")
    args = ap.parse_args()
    frames = []
    for n in [int(x) for x in args.files.split(",")]:
        out = PM / f"parsed_{n:04d}.parquet"
        if not out.exists():
            parse(download(n)).to_parquet(out, index=False)
        frames.append(pd.read_parquet(out))
    df = pd.concat(frames, ignore_index=True)
    df["stem"] = df.stem_title | df.stem_abstract
    pool = pool_dois()
    df["in_pool_doi"] = df.doi.isin(pool)
    hits = df[df.stem].copy()
    oa = openalex_by_pmid(hits.pmid.tolist())
    for k in ("oa_id", "oa_has_abstract", "oa_gate", "oa_type", "oa_doi"):
        hits[k] = hits.pmid.map(lambda p: (oa.get(p) or {}).get(k))
    hits["in_pool"] = hits.in_pool_doi | hits.oa_doi.isin(pool)
    hits.to_parquet(HERE / "cache" / "pubmed_stem_hits.parquet", index=False)
    df.drop(columns=["title"]).to_parquet(HERE / "cache" / "pubmed_sample.parquet", index=False)
    print("records", len(df), "with abstract", df.has_abstract.mean().round(3),
          "stem hits", len(hits))
    print(hits.groupby(["in_pool", "oa_has_abstract", "oa_gate"], dropna=False).size())


if __name__ == "__main__":
    main()
