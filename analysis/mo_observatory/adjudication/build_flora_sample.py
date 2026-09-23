"""Blinded packets for the works where shipped FLoRA (`data/flora.csv`, human-curated)
and the Observatory name DIFFERENT original papers — the counterpart of
`build_sample.py` for FLoRA's own entries rather than our pipeline's.

Reads `flora_vs_mo_originals.csv` (kind == "different paper"), writes
`packets/flor-NNN.md` and `key_flora.csv` (A/B -> flora/mo). flora.csv carries no
abstract, so the packet takes the first one the abstract store holds for the DOI.

    .venv/bin/python -m analysis.mo_observatory.adjudication.build_flora_sample
"""
from __future__ import annotations

import random
import sqlite3
from pathlib import Path

import pandas as pd

from analysis.mo_observatory.adjudication.build_sample import write_packet
from shared.config import CACHE_DIR
from shared.utils import clean_doi

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[2]
SEED = 20260923
_DOI_URL = r"^https?://(dx\.)?doi\.org/"


def _abstract(con: sqlite3.Connection, doi: str) -> str:
    row = con.execute("SELECT abstract FROM abstracts WHERE ident LIKE ? AND abstract IS NOT NULL "
                      "ORDER BY length(abstract) DESC LIMIT 1", (f"%:{doi}",)).fetchone()
    return row[0] if row else ""


def main() -> None:
    diff = pd.read_csv(HERE / "flora_vs_mo_originals.csv", dtype=str)
    write(list(diff[diff.kind == "different paper"].doi_r), "flor", "key_flora.csv")


def write(works: list[str], prefix: str, key_name: str) -> None:
    """Packets for *works* (replication DOIs in flora.csv), ids `<prefix>-NNN`."""
    fl = pd.read_csv(ROOT / "data/flora.csv", dtype=str, encoding="utf-8-sig").fillna("")
    fl["doi_r"], fl["doi_o"] = fl.doi_r.map(clean_doi), fl.doi_o.map(clean_doi)
    fl = fl[fl.doi_r.isin(works)]
    con = sqlite3.connect(CACHE_DIR / "abstracts.sqlite")
    # write_packet reads our pipeline's column names; FLoRA's rows are renamed onto them.
    ours = pd.DataFrame({"doi_r": fl.doi_r, "title_r": fl.title_r, "authors_r": fl.author_r,
                         "year_r": fl.year_r, "journal_r": fl.journal_r,
                         "abstract_r": [_abstract(con, d) for d in fl.doi_r],
                         "doi_o": fl.doi_o, "title_o": fl.title_o, "authors_o": fl.author_o, "year_o": fl.year_o})
    mo = pd.read_csv(HERE.parent / "replications_database_2026_09_04_184008.csv", dtype=str).fillna("")
    mo = mo[~mo.source.str.contains("FLoRa", case=False)]
    mo["doi_r"] = mo.replication_url.str.replace(_DOI_URL, "", regex=True).map(clean_doi)
    mo["doi_o"] = mo.original_url.str.replace(_DOI_URL, "", regex=True).map(clean_doi)
    mo = mo[mo.doi_r.isin(works)]
    rng = random.Random(SEED)
    keys = []
    for i, w in enumerate(sorted(works), 1):
        iid = f"{prefix}-{i:03d}"
        flora_first = rng.random() < 0.5
        it = pd.Series({"doi_r": w, "stratum": "original", "doi_o": ""})
        ft = write_packet(iid, it, ours, mo, flora_first)
        keys.append({"id": iid, "stratum": "original", "doi_r": w, "doi_o": "",
                     "A": "flora" if flora_first else "mo", "B": "mo" if flora_first else "flora",
                     "has_fulltext": ft, "has_abstract": bool(ours[ours.doi_r == w].abstract_r.iloc[0])})
    k = pd.DataFrame(keys)
    k.to_csv(HERE / key_name, index=False, encoding="utf-8-sig")
    print(f"{len(k)} packets; abstract {k.has_abstract.sum()}, full text {k.has_fulltext.sum()}")


if __name__ == "__main__":
    main()
