"""The DOI registry's own record for a DOI, fetched once and cached — issue #210.

OpenAlex sometimes files a real replication's DOI on an unrelated record (a 1948
cosmic-ray paper under a JEAB 2010 DOI). The only independent witness to what a DOI
names is its REGISTRY: Crossref for most of the pool, DataCite and the smaller
agencies for the rest. This module asks the registry, never OpenAlex — an OpenAlex
`filter=doi:` answer may return the twin itself, which is exactly what is in doubt
(that is why `shared.doi_verify.fetch_doi_metadata`, which falls back to OpenAlex,
is not reused here).

Two routes, cheapest first:

- `api.crossref.org/works/<doi>` with the polite-pool `mailto` — free, and it
  carries `subtitle`, `original-title` and `short-title`, which the comparison needs
  to tell a translation or a subtitle from a different paper.
- on a Crossref 404, `https://doi.org/<doi>` with CSL-JSON content negotiation,
  which every registration agency answers (DataCite for OSF, Zenodo, figshare).

Caching follows CLAUDE.md: an answer is cached (a 404 from BOTH routes is an
answer: `registered: false`), a failure is not. Keyed by the cleaned DOI and a
schema version under `cache/doi_registry/`.
"""

import time
from concurrent.futures import ThreadPoolExecutor
from typing import Iterable, Optional

import requests

from shared.cache import read_cache, write_cache
from shared.config import CACHE_DIR, CROSSREF_RATE_SEC, RESEARCHER_EMAIL, log
from shared.rate_limit import throttle
from shared.utils import cache_key, clean_doi

REGISTRY_CACHE_DIR = CACHE_DIR / "doi_registry"
_HEADERS = {"User-Agent": f"FLoRAExtractor/1.0 (mailto:{RESEARCHER_EMAIL})"}
_RETRY_DELAYS = [0, 1, 2, 4]
_DATE_KEYS = ("published-print", "published-online", "published", "issued", "created")


def _years(msg: dict) -> dict:
    out = {}
    for k in _DATE_KEYS:
        parts = (msg.get(k) or {}).get("date-parts") or []
        if parts and parts[0] and parts[0][0]:
            out[k] = int(parts[0][0])
    return out


def _get(url: str, params: dict, headers: dict) -> "tuple[Optional[dict], bool]":
    """(json, answered). (None, True) is a definitive 404; (None, False) a failure."""
    for delay in _RETRY_DELAYS:
        if delay:
            time.sleep(delay)
        throttle("crossref" if "crossref" in url else "doi_org", CROSSREF_RATE_SEC)
        try:
            r = requests.get(url, params=params, headers=headers, timeout=30)
            if r.status_code == 404:
                return None, True
            if 400 <= r.status_code < 500 and r.status_code != 429:
                return None, False
            r.raise_for_status()
            return r.json(), True
        except Exception as exc:  # noqa: BLE001 — boundary: retried, then reported
            log.warning("registry GET %s failed: %s", url, exc)
    return None, False


def fetch(doi: str) -> Optional[dict]:
    """The registry record *doi* names, or None when no registry answered."""
    doi = clean_doi(doi)
    key = cache_key(doi + "_registry_v1")
    cached = read_cache(REGISTRY_CACHE_DIR, key)
    if cached is not None:
        return cached
    data, answered = _get(f"https://api.crossref.org/works/{doi}",
                          {"mailto": RESEARCHER_EMAIL}, _HEADERS)
    if data:
        msg = data.get("message") or {}
        meta = {
            "doi": doi, "registered": True, "agency": "crossref",
            "titles": msg.get("title") or [],
            "subtitles": msg.get("subtitle") or [],
            "original_titles": msg.get("original-title") or [],
            "short_titles": msg.get("short-title") or [],
            "container": (msg.get("container-title") or [""])[0] if msg.get("container-title") else "",
            "years": _years(msg),
            "type": str(msg.get("type") or ""),
        }
        write_cache(REGISTRY_CACHE_DIR, key, meta)
        return meta
    if not answered:
        return None
    csl, cn_answered = _get(f"https://doi.org/{doi}", {},
                            {**_HEADERS, "Accept": "application/vnd.citationstyles.csl+json"})
    if csl:
        title = csl.get("title") or ""
        meta = {
            "doi": doi, "registered": True, "agency": "content_negotiation",
            "titles": [title] if isinstance(title, str) and title else list(title or []),
            "subtitles": [], "original_titles": [],
            "short_titles": [],
            "container": str(csl.get("container-title") or ""),
            "years": _years(csl),
            "type": str(csl.get("type") or ""),
        }
        write_cache(REGISTRY_CACHE_DIR, key, meta)
        return meta
    if not cn_answered:
        return None
    meta = {"doi": doi, "registered": False, "agency": "", "titles": [],
            "subtitles": [], "original_titles": [], "short_titles": [],
            "container": "", "years": {}, "type": ""}
    write_cache(REGISTRY_CACHE_DIR, key, meta)
    return meta


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


def cached_only(doi: str) -> Optional[dict]:
    """The cached record, or None — never a network call."""
    return read_cache(REGISTRY_CACHE_DIR, cache_key(clean_doi(doi) + "_registry_v1"))


if __name__ == "__main__":
    import sys
    for d in sys.argv[1:]:
        print(fetch(d))
