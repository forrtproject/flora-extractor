"""Row identity for dedup — `shared/row_key.py`.

Kept when the Stage 1 API-harvest sources were deleted: row_keys is what the
snapshot scanner and the CSV indexes dedupe on, and nothing else tests it.
"""
from shared.row_key import row_keys as _row_keys


# ---------------------------------------------------------------------------
# Dedup keys (#53): a row with a stronger identifier must NOT dedupe on title,
# so two distinct DOIs that share a title are both kept.
# ---------------------------------------------------------------------------

def test_row_keys_doi_row_has_no_title_key():
    keys = _row_keys({"doi_r": "10.1/abc", "title_r": "A Shared Title"})
    assert "10.1/abc" in keys
    assert not any(k.startswith("title:") for k in keys)


def test_row_keys_doi_less_row_still_uses_title():
    keys = _row_keys({"title_r": "Only A Title"})
    assert keys == ["title:only a title"]


def test_row_keys_treats_nan_as_missing():
    """A pandas 3 str-dtype column stores missing entries as float NaN, and
    `nan or ""` is truthy — so every URL-less row used to key on the literal
    "url:nan" and collide with every other URL-less row."""
    keys = _row_keys({"doi_r": "10.1/abc", "openalex_id_r": float("nan"),
                      "url_r": float("nan"), "title_r": float("nan")})
    assert keys == ["10.1/abc"]

    assert _row_keys({"doi_r": float("nan"), "openalex_id_r": None,
                      "url_r": None, "title_r": "Only A Title"}) == ["title:only a title"]
    assert _row_keys({c: float("nan") for c in
                      ("doi_r", "openalex_id_r", "url_r", "title_r")}) == []
