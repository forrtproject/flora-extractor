"""One test per seam in validate/sources.py: the provenance contract."""
import json

import pandas as pd

from validate import sources


def test_absent_carries_a_reason_and_never_a_zero():
    prov = sources.absent("routing store", "no store -- run `python -m filter.engine route`")
    assert prov["state"] == "absent"
    assert prov["source"] == "routing store"
    assert "filter.engine route" in prov["reason"]
    assert prov["release_id"] is None


def test_extracted_csv_reports_live_state_and_mtime(tmp_path, monkeypatch):
    csv = tmp_path / "extracted.csv"
    pd.DataFrame({"doi_r": ["10.1/a"], "outcome": ["successful"]}).to_csv(csv, index=False)
    monkeypatch.setattr(sources, "EXTRACTED_PATH", csv)

    df, prov = sources.extracted_csv()

    assert len(df) == 1
    assert prov["state"] == "live"
    assert prov["source"] == "extracted.csv"
    assert prov["as_of"] is not None


def test_extracted_csv_absent_when_file_missing(tmp_path, monkeypatch):
    monkeypatch.setattr(sources, "EXTRACTED_PATH", tmp_path / "nope.csv")

    df, prov = sources.extracted_csv()

    assert df is None
    assert prov["state"] == "absent"
    assert "extract.export" in prov["reason"]


def test_stats_json_surfaces_the_release_and_the_producing_machine(tmp_path, monkeypatch):
    """The bug this redesign exists for: stats.json can come from another machine."""
    p = tmp_path / "stats.json"
    p.write_text(json.dumps({
        "updated_at": "2026-08-16T21:06:14",
        "filtered": {
            "release_id": "f7e4667b6c46",
            "store": "/Users/lukaswallrich/Documents/Coding/flora-extractor/cache/engine/engine.duckdb",
        },
    }), encoding="utf-8")
    monkeypatch.setattr(sources, "STATS_PATH", p)

    data, prov = sources.stats_json()

    assert prov["release_id"] == "f7e4667b6c46"
    assert prov["machine"] == "lukaswallrich"
    assert prov["as_of"] == "2026-08-16T21:06:14"


def test_routing_store_absent_when_duckdb_missing(monkeypatch):
    monkeypatch.setattr(sources, "_duckdb_available", lambda: False)

    con, prov = sources.routing_store()

    assert con is None
    assert prov["state"] == "absent"
    assert "duckdb" in prov["reason"]

