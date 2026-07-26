"""Tests for tools/dedup_preprint_versions.py (issue #17 item 3)."""
import pandas as pd

from tools.dedup_preprint_versions import superseded_indices, _base_and_version


def _df(dois):
    return pd.DataFrame({"doi_r": dois})


def test_keeps_highest_version_when_no_versionless():
    df = _df(["10.31234/osf.io/d3x9p_v1", "10.31234/osf.io/d3x9p_v2",
              "10.31234/osf.io/d3x9p_v4", "10.31234/osf.io/d3x9p_v3"])
    drop = superseded_indices(df)
    kept = set(df.index) - set(drop)
    assert len(kept) == 1
    assert df.loc[next(iter(kept)), "doi_r"] == "10.31234/osf.io/d3x9p_v4"


def test_versionless_doi_wins_over_versions():
    df = _df(["10.31234/osf.io/d3x9p_v1", "10.31234/osf.io/d3x9p",
              "10.31234/osf.io/d3x9p_v2"])
    drop = superseded_indices(df)
    kept = set(df.index) - set(drop)
    assert [df.loc[i, "doi_r"] for i in kept] == ["10.31234/osf.io/d3x9p"]


def test_distinct_works_untouched():
    df = _df(["10.31234/osf.io/aaaaa_v1", "10.31234/osf.io/bbbbb_v1", "10.1/plain"])
    assert superseded_indices(df) == []  # each base has one row


def test_single_version_row_untouched():
    df = _df(["10.31234/osf.io/d3x9p_v1"])
    assert superseded_indices(df) == []


def test_base_and_version_parsing():
    assert _base_and_version("10.31234/osf.io/d3x9p_v4") == ("10.31234/osf.io/d3x9p", 4)
    assert _base_and_version("10.1037/xge0000123") == ("10.1037/xge0000123", None)
