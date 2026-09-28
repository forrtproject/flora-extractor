"""The DOI registry's own record for a DOI, fetched once and cached — issue #210.

The fetcher moved to `shared/doi_registry.py` when Stage 3 started using it as a
guard (ladder 30); this module keeps the audit's batch helper and re-exports the rest,
so the audit and the guard read and write one cache under one schema.
"""

from concurrent.futures import ThreadPoolExecutor
from typing import Iterable, Optional

from shared.config import log
from shared.doi_registry import REGISTRY_CACHE_DIR, cached_only, fetch  # noqa: F401
from shared.utils import clean_doi


def fetch_many(dois: Iterable[str], workers: int = 4) -> dict[str, Optional[dict]]:
    """Registry records for *dois*; the throttle keeps the workers on one rate."""
    unique = sorted({clean_doi(d) for d in dois if d and clean_doi(d)})
    done = 0

    def one(doi: str) -> "tuple[str, Optional[dict]]":
        nonlocal done
        meta = fetch(doi)
        done += 1
        if done % 500 == 0:
            log.info("registry: %d / %d", done, len(unique))
        return doi, meta

    with ThreadPoolExecutor(max_workers=workers) as pool:
        return dict(pool.map(one, unique))


if __name__ == "__main__":
    import sys
    for d in sys.argv[1:]:
        print(fetch(d))
