"""Draw a probability-proportional-to-size sample of the OpenAlex works snapshot.

Read-only, over public HTTPS range requests: for each of N partition files drawn with
probability proportional to its record_count, ONE random row group is read with a
column projection (no full-file download). Every row is reduced to the features the
pre-gate narrowing experiment needs, plus the three search-gate arms computed exactly
as `search/snapshot_scan.py` computes them (stem over display_name/title, stem over the
RAW abstract_inverted_index JSON, CONCEPT_IDS over concepts[].id).

    .venv/bin/python -m analysis.recall_pregate.sample_snapshot --files 64
Output: analysis/recall_pregate/cache/sample/rg_<file>_<rg>.parquet (resumable).
"""

import argparse
from concurrent.futures import ThreadPoolExecutor
import json
import random
import re
from pathlib import Path

import fsspec
import numpy as np
import pyarrow as pa
import pyarrow.compute as pc
import pyarrow.parquet as pq

from filter.phrase_detection import CONCEPT_IDS, REPLICATION_STEM_PATTERN

HERE = Path(__file__).resolve().parent
MANIFEST = HERE / "cache" / "manifest.json"
OUT = HERE / "cache" / "sample"
HTTPS = "https://openalex.s3.amazonaws.com/"

COLS = ["id", "doi", "display_name", "title", "ids", "indexed_in", "language", "type",
        "publication_year", "primary_topic", "is_paratext", "is_retracted",
        "referenced_works_count", "abstract_inverted_index", "has_content",
        "primary_location", "concepts", "cited_by_count"]

_CONCEPTS = {f"https://openalex.org/{c}" for c in CONCEPT_IDS} | set(CONCEPT_IDS)
_DOI_PREFIX = re.compile(r"(10\.\d{4,9})/")


def _reduce(tb: pa.Table, url: str, rg: int) -> pa.Table:
    title = pc.coalesce(tb["display_name"], tb["title"])
    abstract = tb["abstract_inverted_index"]
    has_abs = pc.and_(pc.is_valid(abstract),
                      pc.greater(pc.utf8_length(pc.fill_null(abstract, "")), 2))
    hit_title = pc.fill_null(pc.match_substring_regex(title, REPLICATION_STEM_PATTERN), False)
    hit_abs = pc.fill_null(pc.match_substring_regex(abstract, REPLICATION_STEM_PATTERN), False)
    abs_len = pc.fill_null(pc.utf8_length(abstract), 0)

    rows = tb.drop_columns(["abstract_inverted_index", "title"]).to_pylist()
    out = []
    for r, ha, ht, hab, al in zip(rows, has_abs.to_pylist(), hit_title.to_pylist(),
                                  hit_abs.to_pylist(), abs_len.to_pylist()):
        ids = dict(r["ids"] or [])
        pt = r["primary_topic"] or {}
        pl = r["primary_location"] or {}
        src = pl.get("source") or {}
        doi = (r["doi"] or "").lower().replace("https://doi.org/", "")
        m = _DOI_PREFIX.match(doi)
        hc = r["has_content"] or {}
        out.append({
            "id": r["id"], "doi": doi or None, "doi_prefix": m.group(1) if m else None,
            "title": r["display_name"], "language": r["language"], "type": r["type"],
            "year": r["publication_year"], "has_pmid": bool(ids.get("pmid")),
            "has_pmcid": bool(ids.get("pmcid")), "has_mag": bool(ids.get("mag")),
            "indexed_in": "|".join(sorted(r["indexed_in"] or [])),
            "domain": ((pt.get("domain") or {}).get("display_name")),
            "field": ((pt.get("field") or {}).get("display_name")),
            "subfield": ((pt.get("subfield") or {}).get("display_name")),
            "topic": pt.get("display_name"),
            "source_type": src.get("type"), "source_name": src.get("display_name"),
            "host_org": src.get("host_organization_name"),
            "is_core": src.get("is_core"),
            "is_paratext": r["is_paratext"], "is_retracted": r["is_retracted"],
            "refs": r["referenced_works_count"] or 0,
            "cited_by": r["cited_by_count"] or 0,
            "has_pdf": bool(hc.get("pdf")), "has_grobid": bool(hc.get("grobid_xml")),
            "has_abstract": ha, "abstract_len": al,
            "hit_title": ht, "hit_abstract": hab,
            "hit_concept": any(c.get("id") in _CONCEPTS for c in (r["concepts"] or [])),
            "file": url.rsplit("works/", 1)[-1], "rg": rg,
        })
    return pa.Table.from_pylist(out)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--files", type=int, default=64)
    ap.add_argument("--workers", type=int, default=6)
    ap.add_argument("--seed", type=int, default=20260923)
    args = ap.parse_args()
    OUT.mkdir(parents=True, exist_ok=True)
    files = json.load(open(MANIFEST))["files"]
    weights = np.array([f["meta"]["record_count"] for f in files], dtype=float)
    rng = np.random.default_rng(args.seed)
    picks = rng.choice(len(files), size=args.files, replace=False, p=weights / weights.sum())
    fs = fsspec.filesystem("https")

    def one(i: int, idx: int) -> None:
        url = files[idx]["url"].replace("s3://openalex/", HTTPS)
        pf = pq.ParquetFile(fs.open(url, "rb", block_size=8 * 2**20))
        rg = random.Random(args.seed + int(idx)).randrange(pf.metadata.num_row_groups)
        target = OUT / f"rg_{idx:04d}_{rg}.parquet"
        if target.exists():
            return
        tb = pf.read_row_group(rg, columns=COLS)
        red = _reduce(tb, url, rg)
        pq.write_table(red, target.with_suffix(".tmp"))
        target.with_suffix(".tmp").rename(target)
        print(f"{i + 1}/{len(picks)} {url.rsplit('works/', 1)[-1]} rg{rg} rows={red.num_rows}",
              flush=True)

    with ThreadPoolExecutor(args.workers) as pool:
        list(pool.map(lambda a: one(*a), enumerate(sorted(picks))))


if __name__ == "__main__":
    main()
