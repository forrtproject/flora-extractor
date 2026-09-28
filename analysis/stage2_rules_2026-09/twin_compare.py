"""Does an OpenAlex record describe the paper its DOI is registered to? — issue #210.

The comparison moved to `shared/doi_registry.py` (see its docstring for the scoring and
the measurement) when Stage 3 started using it as a guard; re-exported here so the
audit scripts keep their imports.
"""

from shared.doi_registry import latin_share, overlap, registry_titles, similarity, tokens, year_gap  # noqa: F401
