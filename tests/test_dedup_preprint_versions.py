"""Tests for tools/dedup_preprint_versions.py (issue #17 item 3)."""
import pandas as pd
import pytest

from tools.dedup_preprint_versions import superseded_indices, _base_and_version


def _df(dois):
    return pd.DataFrame({"doi_r": dois})


@pytest.mark.parametrize("dois,survivor", [
    # No versionless DOI: the highest version wins.
    (["10.31234/osf.io/d3x9p_v1", "10.31234/osf.io/d3x9p_v2",
      "10.31234/osf.io/d3x9p_v4", "10.31234/osf.io/d3x9p_v3"],
     "10.31234/osf.io/d3x9p_v4"),
    # A versionless DOI beats every numbered version.
    (["10.31234/osf.io/d3x9p_v1", "10.31234/osf.io/d3x9p",
      "10.31234/osf.io/d3x9p_v2"],
     "10.31234/osf.io/d3x9p"),
])
def test_one_survivor_per_base(dois, survivor):
    df = _df(dois)
    kept = set(df.index) - set(superseded_indices(df))
    assert [df.loc[i, "doi_r"] for i in kept] == [survivor]


def test_distinct_works_untouched():
    """Each base has exactly one row — including a lone _v1."""
    df = _df(["10.31234/osf.io/aaaaa_v1", "10.31234/osf.io/bbbbb_v1", "10.1/plain"])
    assert superseded_indices(df) == []


def test_base_and_version_parsing():
    assert _base_and_version("10.31234/osf.io/d3x9p_v4") == ("10.31234/osf.io/d3x9p", 4)
    assert _base_and_version("10.1037/xge0000123") == ("10.1037/xge0000123", None)
