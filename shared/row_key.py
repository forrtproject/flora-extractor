"""
row_key.py — the one definition of "which paper is this row?".

One fallback chain, in one place, so that a row a stage has already seen is
recognised by the same identifier wherever the question is asked.

``row_keys`` returns every key a row can be recognised by, strongest first, for a
caller that wants a match on ANY identifier. ``primary_key`` returns the strongest
alone; `_cache_id` in `extract/run_extract.py` is its live consumer, naming the
parse cache entry of a row that has no DOI.

The key STRINGS are part of the on-disk format. A cache entry filed under
``oa:``/``url:``/``title:`` is found again only by the same prefix, so changing one
orphans every entry written with it.
"""

import math

from shared.utils import clean_doi


def _text(value: object) -> str:
    """*value* as a string, with missing values as "".

    A pandas 3 str-dtype column holds missing entries as float NaN, and
    ``float("nan") or ""`` is truthy — so ``str(row.get(...) or "")`` yielded the
    literal "nan" and every URL-less row collided on the single key ``url:nan``.
    """
    if value is None:
        return ""
    if isinstance(value, float) and math.isnan(value):
        return ""
    return str(value)


def row_keys(row: "dict") -> list[str]:
    """All identifying keys for *row*, strongest identifier first.

    Priority: doi → openalex_id → url → title. Title is a LAST-RESORT identifier
    (#53): a row carrying any other identifier must never contribute a title key,
    otherwise two distinct works sharing a title (Reply/Commentary pairs,
    "Registered Replication Report" stubs, identically-titled corrections) collide
    and the second is silently dropped.
    """
    keys: list[str] = []
    doi = clean_doi(_text(row.get("doi_r", "")))
    if doi:
        keys.append(doi)
    oa = _text(row.get("openalex_id_r", "")).strip()
    if oa:
        keys.append(f"oa:{oa}")
    url = _text(row.get("url_r", "")).strip()
    if url:
        keys.append(f"url:{url}")
    title = _text(row.get("title_r", "")).lower().strip()
    if title and not keys:
        keys.append(f"title:{title}")
    return keys


def primary_key(row: "dict") -> str:
    """The single strongest identifying key for *row*, or "" if it has none."""
    keys = row_keys(row)
    return keys[0] if keys else ""
