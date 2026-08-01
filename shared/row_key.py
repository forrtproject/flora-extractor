"""
row_key.py — the one definition of "which paper is this row?".

Stages 1, 2 and 3 each need to recognise a row they have already seen, and each
had grown its own version of the same fallback chain — with two different
priority orders, so the same row could be filed under one identifier in
candidates.csv and a different one in filtered.csv.

The key STRINGS are part of the on-disk format: cache/candidates_index.txt and
cache/filtered_index.txt were written with the ``oa:``/``url:``/``title:``
prefixes below, so those must not change or every existing index is invalidated.

``row_keys`` returns every key a row can be recognised by (the candidates index
stores all of them, so a duplicate is caught via any identifier);
``primary_key`` returns the first, which is what the single-key resume indexes
store.
"""

from shared.utils import clean_doi


def row_keys(row: "dict") -> list[str]:
    """All identifying keys for *row*, strongest identifier first.

    Priority: doi → openalex_id → url → title. Title is a LAST-RESORT identifier
    (#53): a row carrying any other identifier must never contribute a title key,
    otherwise two distinct works sharing a title (Reply/Commentary pairs,
    "Registered Replication Report" stubs, identically-titled corrections) collide
    and the second is silently dropped.
    """
    keys: list[str] = []
    doi = clean_doi(str(row.get("doi_r", "") or ""))
    if doi:
        keys.append(doi)
    oa = str(row.get("openalex_id_r", "") or "").strip()
    if oa:
        keys.append(f"oa:{oa}")
    url = str(row.get("url_r", "") or "").strip()
    if url:
        keys.append(f"url:{url}")
    title = str(row.get("title_r", "") or "").lower().strip()
    if title and not keys:
        keys.append(f"title:{title}")
    return keys


def primary_key(row: "dict") -> str:
    """The single strongest identifying key for *row*, or "" if it has none."""
    keys = row_keys(row)
    return keys[0] if keys else ""
