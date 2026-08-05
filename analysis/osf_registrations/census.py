"""Per-template census of the OSF no-abstract population, with the FLoRA intersection.

The evidence `osf-registration-protocol` is shadow for (REPORT.md): not a sample
of templates but the whole population, each row put through the two shipped specs
by the engine's own evaluator, and each checked against `data/flora.csv` — the
published FLoRA database, this project's only gold standard.

    python analysis/osf_registrations/census.py [OVERLAY_DIR] [WORKLIST]
"""
import sys
from pathlib import Path

import pandas as pd
import pyarrow.parquet as pq

from filter.engine.backends import eval_spec_rows
from filter.engine.overlay import chunk_paths
from filter.engine.spec import load_specs
from search.fetch_abstracts import OSF_TEMPLATE_PREFIX
from shared.config import BASE_DIR
from shared.flora_skip import _osf_doi_keys
from shared.utils import clean_doi

HERE = Path(__file__).resolve().parent
ADMIT, DISCARD = "osf-registration-completed", "osf-registration-protocol"


def main(overlay_dir: Path, worklist_path: Path) -> int:
    worklist = {r["work_id"]: r for r in pq.read_table(worklist_path).to_pylist()}
    overlay: dict[int, str] = {}
    for path in chunk_paths(overlay_dir):
        table = pq.read_table(path, columns=["work_id", "abstract_text"])
        overlay.update(zip(table.column("work_id").to_pylist(),
                           table.column("abstract_text").to_pylist()))

    # Both spellings, or the recall check under-counts by half: FLoRA names an
    # OSF record by URL far more often than by DOI (366 rows against 51 over
    # flora.csv), and `_osf_doi_keys()` is the one place that mapping lives.
    flora = pd.read_csv(BASE_DIR / "data" / "flora.csv", low_memory=False)
    flora_dois = {clean_doi(str(d))
                  for column in ("doi_r", "alt_identifier_r", "doi_o",
                                 "alt_identifier_o")
                  for d in flora[column].dropna()}
    flora_dois |= _osf_doi_keys(flora)

    specs = load_specs(BASE_DIR / "filter" / "spec")
    rows, records = [], []
    for wid, meta in worklist.items():
        text = overlay.get(wid) or ""
        template = (text.split("\n", 1)[0][len(OSF_TEMPLATE_PREFIX):].strip()
                    if text.startswith(OSF_TEMPLATE_PREFIX) else "")
        rows.append({"id": f"https://openalex.org/W{wid}", "doi": meta["doi"],
                     "title": meta["title"], "display_name": meta["title"],
                     "publication_year": meta["year"], "type": "article",
                     "authorships": "[]", "primary_location": "{}", "open_access": "{}",
                     "concepts": "[]", "abstract_text": text,
                     "hit_token_title": True, "hit_token_abstract": True,
                     "hit_concept": False})
        records.append({"work_id": wid, "doi": meta["doi"], "title": meta["title"],
                        "template": template, "fetched": bool(text),
                        "in_flora": clean_doi(meta["doi"]) in flora_dois,
                        "chars": len(text)})

    for spec_id in (ADMIT, DISCARD):
        spec = next(s for s in specs if s.id == spec_id)
        for rec, hit in zip(records, eval_spec_rows(spec, rows)):
            rec[spec_id] = bool(hit)

    df = pd.DataFrame(records)
    df["verdict"] = ["admit" if a else "discard" if d else "untouched"
                     for a, d in zip(df[ADMIT], df[DISCARD])]

    print(f"population: {len(df):,} no-abstract rows on the OSF registrant")
    print(f"  registration text recovered: {df.fetched.sum():,}"
          f"   (no registration / 404: {(~df.fetched).sum():,})")
    print(f"  known FLoRA papers in the population: {df.in_flora.sum()}\n")
    print(df.groupby(["template", "verdict"]).agg(
        rows=("work_id", "size"), flora=("in_flora", "sum"),
        median_chars=("chars", "median")).reset_index()
        .sort_values("rows", ascending=False).to_string(index=False))
    print("\nverdict totals:")
    print(df.groupby("verdict").agg(rows=("work_id", "size"),
                                    flora=("in_flora", "sum")).to_string())
    print("\nevery known FLoRA row, with the template it is on:")
    print(df[df.in_flora][["doi", "template", "verdict"]].to_string(index=False))

    out = HERE / "census.csv"
    df[["work_id", "doi", "template", "verdict", "in_flora", "fetched",
        "chars"]].to_csv(out, index=False)
    print(f"\nwrote {out}")
    return 0


if __name__ == "__main__":
    overlay = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("/tmp/osf_overlay")
    worklist = Path(sys.argv[2]) if len(sys.argv) > 2 else Path("/tmp/osf_worklist.parquet")
    sys.exit(main(overlay, worklist))
