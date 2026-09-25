"""What a DOI's REGISTRY says it names, and whether that is the row's own paper — issue #210.

OpenAlex sometimes files a real replication under an unrelated paper's DOI (a JEAB
2010 DOI on a 1948 cosmic-ray paper; an aphasia replication under a diabetes-care
review). Stage 3 then fetches that other paper through every DOI-keyed step — the
Europe PMC full text, Unpaywall, the publisher's PDF, OpenCitations' reference list —
and codes the replication from someone else's text. The only independent witness to
what a DOI names is its registry: Crossref for most of the pool, DataCite and the
smaller agencies for the rest. This module asks the registry, never OpenAlex — an
OpenAlex `filter=doi:` answer may return the mis-filed record itself, which is exactly
what is in doubt (why `shared.doi_verify.fetch_doi_metadata`, which falls back to
OpenAlex, is not reused).

Two routes, cheapest first:

- `api.crossref.org/works/<doi>` with the polite-pool `mailto` — free, and it carries
  `subtitle`, `original-title` and `short-title`, which the comparison needs to tell a
  translation or a subtitle from a different paper.
- only on a Crossref 404, `https://doi.org/<doi>` with CSL-JSON content negotiation,
  which every registration agency answers (DataCite for OSF, Zenodo, figshare).

An answer is cached (a 404 from BOTH routes is an answer: `registered: false`); a
failure is not. The cache is the one the issue #210 audit filled
(`analysis/stage2_rules_2026-09/doi_twins_audit.py`, same key and schema), so the
7,400 DOIs it compared are already hits.

`check()` is the comparison and the audit's verdict rule, measured there: all 30 known
twin records score 0.0, every one of the 7,285 matching works scores ≥ 0.867, so any
cut in 0.15–0.85 separates them; 0.5 is used. The score is the token OVERLAP
COEFFICIENT (|A ∩ B| / min(|A|, |B|)) against every title the registry holds — main,
main + subtitle, original-language, short — so a preprint titled "X" and its article
"X: evidence from three experiments" score 1.0 where Jaccard would penalise them. A
title in another script than the registry's is a translation, not a mismatch, unless
the years are more than one apart.
"""

import html
import re
import time
import unicodedata
from typing import Optional

import requests

from shared.cache import read_cache, write_cache
from shared.config import CACHE_DIR, CROSSREF_RATE_SEC, RESEARCHER_EMAIL, log
from shared.rate_limit import throttle
from shared.utils import cache_key, clean_doi

REGISTRY_CACHE_DIR = CACHE_DIR / "doi_registry"
_HEADERS = {"User-Agent": f"FLoRAExtractor/1.0 (mailto:{RESEARCHER_EMAIL})"}
_RETRY_DELAYS = [0, 1, 2, 4]
_DATE_KEYS = ("published-print", "published-online", "published", "issued", "created")

# Above this the titles name the same paper; see the module docstring for the measurement.
SIM_MAX = 0.5


def _key(doi: str) -> str:
    return cache_key(doi + "_registry_v1")


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


def _record(doi: str, agency: str, titles: list, msg: dict, *, subtitles: list = (),
            original_titles: list = (), short_titles: list = ()) -> dict:
    container = msg.get("container-title") or ""
    if isinstance(container, list):
        container = container[0] if container else ""
    return {"doi": doi, "registered": True, "agency": agency, "titles": list(titles),
            "subtitles": list(subtitles), "original_titles": list(original_titles),
            "short_titles": list(short_titles), "container": str(container),
            "years": _years(msg), "type": str(msg.get("type") or "")}


def fetch(doi: str) -> Optional[dict]:
    """The registry record *doi* names, or None when no registry answered."""
    doi = clean_doi(doi)
    if not doi:
        return None
    cached = read_cache(REGISTRY_CACHE_DIR, _key(doi))
    if cached is not None:
        return cached
    data, answered = _get(f"https://api.crossref.org/works/{doi}",
                          {"mailto": RESEARCHER_EMAIL}, _HEADERS)
    if data:
        msg = data.get("message") or {}
        meta = _record(doi, "crossref", msg.get("title") or [], msg,
                       subtitles=msg.get("subtitle") or [],
                       original_titles=msg.get("original-title") or [],
                       short_titles=msg.get("short-title") or [])
        write_cache(REGISTRY_CACHE_DIR, _key(doi), meta)
        return meta
    if not answered:
        return None
    csl, cn_answered = _get(f"https://doi.org/{doi}", {},
                            {**_HEADERS, "Accept": "application/vnd.citationstyles.csl+json"})
    if csl:
        title = csl.get("title") or ""
        meta = _record(doi, "content_negotiation",
                       [title] if isinstance(title, str) and title else list(title or []),
                       csl)
        write_cache(REGISTRY_CACHE_DIR, _key(doi), meta)
        return meta
    if not cn_answered:
        return None
    meta = {"doi": doi, "registered": False, "agency": "", "titles": [],
            "subtitles": [], "original_titles": [], "short_titles": [],
            "container": "", "years": {}, "type": ""}
    write_cache(REGISTRY_CACHE_DIR, _key(doi), meta)
    return meta


def cached_only(doi: str) -> Optional[dict]:
    """The cached record, or None — never a network call."""
    return read_cache(REGISTRY_CACHE_DIR, _key(clean_doi(doi)))


# ── The comparison ─────────────────────────────────────────────────────────────

_TAG = re.compile(r"<[^>]+>")
_TOKEN = re.compile(r"[^\W_]+", re.UNICODE)
_STOP = frozenset("""a an and the of in on for to with by from at as is are was were be
    or its their our we vs versus into than that this these those via""".split())


def tokens(title: str) -> set[str]:
    text = html.unescape(_TAG.sub(" ", title or ""))
    text = unicodedata.normalize("NFKD", text)
    text = "".join(c for c in text if not unicodedata.combining(c)).lower()
    return {t for t in _TOKEN.findall(text) if t not in _STOP and (len(t) > 1 or not t.isascii())}


def registry_titles(meta: dict) -> list[str]:
    mains = list(meta.get("titles") or [])
    subs = list(meta.get("subtitles") or [])
    out = mains + [f"{m} {s}" for m in mains for s in subs]
    out += list(meta.get("original_titles") or []) + list(meta.get("short_titles") or [])
    return [t for t in out if t]


def overlap(a: set[str], b: set[str]) -> float:
    if not a or not b:
        return float("nan")
    return len(a & b) / min(len(a), len(b))


def similarity(title: str, meta: dict) -> float:
    """Best overlap coefficient against any registry title; NaN when either side is empty."""
    a = tokens(title)
    scores = [overlap(a, tokens(t)) for t in registry_titles(meta)]
    scores = [s for s in scores if s == s]
    return max(scores) if scores else float("nan")


def year_gap(year, meta: dict) -> Optional[int]:
    dates = meta.get("years") or {}
    # `created` is when the DOI was REGISTERED — a 1985 article deposited in 2002 has
    # created=2002 — so it counts only when the registry states no publication date.
    years = [y for k, y in dates.items() if y and k != "created"] or \
        [y for y in dates.values() if y]
    try:
        y0 = int(float(year))
    except (TypeError, ValueError):
        return None
    return min(abs(y0 - y) for y in years) if years else None


def latin_share(title: str) -> float:
    letters = [c for c in title or "" if c.isalpha()]
    if not letters:
        return 1.0
    return sum(1 for c in letters if "LATIN" in unicodedata.name(c, "")) / len(letters)


def check(doi: str, title: str, year=None, *, network: bool = True) -> dict:
    """Does *doi*'s registry record name the paper titled *title*?

    `verdict` is one of: `mismatch` (the only one that means "another paper"),
    `match`, `translation_suspect`, `no_title` (either side has no comparable title),
    `unregistered`, `unanswered` (no registry answered — a failure, never a
    mismatch) and `no_doi`. `network=False` reads the cache only; a miss is then
    `unanswered`.
    """
    doi = clean_doi(doi)
    if not doi:
        return {"verdict": "no_doi"}
    meta = fetch(doi) if network else cached_only(doi)
    if meta is None:
        return {"verdict": "unanswered"}
    if not meta.get("registered"):
        return {"verdict": "unregistered"}
    reg_title = (meta.get("titles") or [""])[0]
    sim = similarity(title, meta)
    gap = year_gap(year, meta)
    out = {"registry_title": reg_title, "similarity": sim, "year_gap": gap,
           "agency": meta.get("agency", ""), "titles": registry_titles(meta)}
    if sim != sim:
        return {**out, "verdict": "no_title"}
    if sim > SIM_MAX:
        return {**out, "verdict": "match"}
    # The registry may carry only the English title of a paper OpenAlex holds in its
    # own language: a different script is a translation until the year says otherwise.
    if abs(latin_share(title) - latin_share(reg_title)) > 0.5 and (gap is None or gap <= 1):
        return {**out, "verdict": "translation_suspect"}
    return {**out, "verdict": "mismatch"}


# ── Whose paper an abstract describes ─────────────────────────────────────────


def title_coverage(title: str, text: str) -> float:
    """The share of *title*'s content tokens that occur in *text*; 0.0 for an empty title."""
    t = tokens(title)
    return len(t & tokens(text)) / len(t) if t else 0.0


def abstract_names_registry(abstract: str, title: str, reg_titles: list[str]) -> dict:
    """Does *abstract* describe the registry's paper rather than the one titled *title*?

    Asked only on a `mismatch`: the DOI already names another paper, and the question
    is whether OpenAlex's abstract came with the DOI (it describes the registry record)
    or with the title (it is the row's own). An abstract restates its own paper's
    title words, so the side whose title it covers MORE is the paper it describes; a
    tie — including an abstract that shares nothing with either — keeps it.

    Measured 2026-09-25 over the 40 audit mismatches (issue #210: 30 twin records, 8
    real studies under another paper's DOI, 2 withdrawn preprints), each abstract
    read by hand: the 38 describing the registry's paper cover 0.17–1.00 of a
    registry title and at most 0.25 of their own; the 2 describing the row's own
    paper cover 0.57 and 0.93 of their own title and at most 0.14 of the registry's.
    No threshold to tune — every case sits at least 0.17 from the tie.
    """
    own = title_coverage(title, abstract)
    registry = max((title_coverage(t, abstract) for t in reg_titles), default=0.0)
    return {"own": own, "registry": registry, "names_registry": registry > own}
