"""Live check of the OpenAlex parquet snapshot scanner against the real bucket.

Pinned to the smallest partition in the manifest (updated_date=2016-06-24,
~3.6 MB, 2,439 records) so the run stays seconds long. The scan is ledger-backed
and there is no sample mode any more, so the ledger and the pool are redirected
into tmp_path here — a live run must never mark a real partition done or write
into the production pool.
"""

import os

import pandas as pd
import pytest

from search import snapshot_scan as ss
from search.snapshot_scan import fetch_manifest, scan_snapshot

_PARTITION = ("https://openalex.s3.amazonaws.com/data/parquet/works/"
              "updated_date=2016-06-24/part_0000.parquet")


@pytest.mark.skipif(
    not os.getenv("TEST_LIVE_API"),
    reason="set TEST_LIVE_API=1 to run live API tests",
)
class TestSnapshotLive:

    def test_manifest_lists_parquet_partitions(self):
        manifest = fetch_manifest(refresh=True)
        urls = [f if isinstance(f, str) else (f.get("url") or f.get("key"))
                for f in manifest["files"]]
        assert len(urls) > 100
        assert all(u.startswith("https://openalex.s3.amazonaws.com/") for u in urls)
        assert _PARTITION in urls

    def test_one_partition_yields_survivors_in_schema(self, tmp_path, monkeypatch):
        monkeypatch.setattr(ss, "_LEDGER_PATH", tmp_path / "ledger.json")
        pool = tmp_path / "pool"

        n = scan_snapshot(files=[_PARTITION], survivor_pool=pool)

        assert n > 0, "the 2016-06-24 partition contains replication papers"
        df = pd.read_parquet(pool)
        assert list(df.columns) == ss._POOL_SCHEMA.names
        assert len(df) == n
        assert df[["hit_token_title", "hit_token_abstract", "hit_concept"]].any(axis=1).all()
