"""Build the backfill worklist for the OSF no-abstract population, off the pool.

Not `filter.engine.overlay.worklist()`: that one takes the `no_text` rows of a
routing release, and this population is defined independently of any release —
every pool row on the OSF registrant whose abstract is empty. Counted on disk.

    python analysis/osf_registrations/build_worklist.py /tmp/osf_worklist.parquet
"""
import sys
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq

from filter.engine.overlay import WORKLIST_SCHEMA
from filter.engine.workids import work_id
from search.fetch_abstracts import OSF_REGISTRANT
from shared.config import SNAPSHOT_POOL_DIR
from shared.utils import clean_doi

COLS = ["id", "doi", "title", "display_name", "publication_year", "abstract_text"]


def main(out_path: Path) -> int:
    rows: list[dict] = []
    seen: set[int] = set()
    on_registrant = 0
    paths = sorted(SNAPSHOT_POOL_DIR.glob("*.parquet"))
    for n, path in enumerate(paths, 1):
        for batch in pq.ParquetFile(path).iter_batches(batch_size=50_000, columns=COLS):
            for rec in batch.to_pylist():
                doi = clean_doi(rec.get("doi") or "")
                if doi.split("/", 1)[0] != OSF_REGISTRANT:
                    continue
                on_registrant += 1
                if (rec.get("abstract_text") or "").strip():
                    continue
                wid = work_id(rec["id"])
                if wid in seen:
                    continue
                seen.add(wid)
                rows.append({"work_id": wid, "doi": doi,
                             "title": rec.get("display_name") or rec.get("title") or "",
                             "year": rec.get("publication_year")})
        if n % 250 == 0:
            print(f"  {n}/{len(paths)} files, {len(rows):,} no-abstract rows", flush=True)

    out_path.parent.mkdir(parents=True, exist_ok=True)
    pq.write_table(pa.Table.from_pylist(rows, schema=WORKLIST_SCHEMA), out_path)
    print(f"pool rows on {OSF_REGISTRANT}: {on_registrant:,}")
    print(f"  of which no abstract: {len(rows):,} -> {out_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main(Path(sys.argv[1])))
