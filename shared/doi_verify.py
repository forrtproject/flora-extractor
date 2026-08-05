"""
doi_verify.py — Verify that a resolved doi_o actually points to the resolved
original study.

LLM resolution can hallucinate DOIs: title/author correct but the DOI pointing
to a different (yet perfectly registered) paper. A doi.org resolution check
cannot catch this, so we fetch the metadata the DOI points to (CrossRef, the
registry of record; OpenAlex fallback) and compare title/year. On mismatch we
re-resolve the DOI from title+author via CrossRef bibliographic search, with an
OpenAlex fallback because OpenAlex also indexes DOI-less works (old papers,
book chapters).

Cost: the three search tiers of verify_and_correct issue up to three OpenAlex
free-text `search` queries per row, each billed at 10× a filter query. That makes
this module the dominant OpenAlex line of a long Stage 3 run, which is why
run_extract does not re-verify a carried-forward row whose verification already
settled (see _needs_verification there) and why the searches run through
openalex_client._oa_get, which throttles them, rotates keys, counts them
(search_query_count()) and raises OpenAlexQuotaExhausted rather than letting an
unaffordable request read as "no match".
"""
from __future__ import annotations

import re
import time
from typing import Optional

import requests

from shared.cache import read_cache, write_cache
from shared.config import (
    CROSSREF_RATE_SEC, DOI_VERIFY_CACHE_DIR, RESEARCHER_EMAIL, log,
)
from shared.disambiguation import jaccard_similarity
from shared.openalex_client import _oa_get, author_matches
from shared.utils import bare_work_id, cache_key, clean_doi

# Tunable thresholds — starting points, not yet empirically validated.
VERIFY_TITLE_JACCARD  = 0.5   # DOI metadata vs resolved title → "verified"
RESOLVE_TITLE_JACCARD = 0.7   # search hit vs resolved title → safe to auto-correct
YEAR_TOLERANCE        = 1     # print vs online publication year offset
TITLE_ONLY_JACCARD    = 0.6   # last-resort tier: LLM titles are often paraphrased
TITLE_ONLY_GAP        = 1.5   # ... and must dominate the second-best hit by this factor

_HEADERS      = {"User-Agent": f"FLoRAExtractor/1.0 (mailto:{RESEARCHER_EMAIL})"}
_RETRY_DELAYS = [0, 1, 2, 4]  # initial attempt + 3 retries per CLAUDE.md

# Correction notices embed the article title and score high on Jaccard, but
# can never be the original study.
_ERRATA_RE = re.compile(
    r"^\s*(corrigendum|erratum|correction|retraction|expression\s+of\s+concern|"
    r"publisher\s+correction)\b", re.IGNORECASE)


class _Unverifiable(dict):
    """A search result that is not a result: no source answered.

    It is FALSY on purpose. Every caller of resolve_doi_by_metadata already writes
    `if hit:` and does the conservative thing when there is no hit — no provisional
    DOI, no correction — and that is exactly right for "we could not ask". A caller
    that must tell an outage from a genuine no-match checks `is UNVERIFIABLE`
    explicitly, and only verify_and_correct does, because only it turns the answer
    into a permanent verdict on the row.
    """

    def __bool__(self) -> bool:
        return False


# The single instance; compare with `is`, never by truthiness or equality.
UNVERIFIABLE = _Unverifiable(unverifiable=True)


def _get_json(url: str, params: dict) -> "tuple[Optional[dict], bool]":
    """GET *CrossRef* with retries. Returns (json, not_found).

    (None, True)  → HTTP 404 (a definitive answer, not retried)
    (None, False) → hard failure after all retries

    OpenAlex is NOT called from here: it is metered per request against a daily
    budget, so its calls go through openalex_client._oa_get, which shares the one
    reservation queue (`throttle`), rotates through the key module on exhaustion, and
    raises OpenAlexQuotaExhausted rather than handing this module a None that would
    read as "no match". This function used to attach the first OpenAlex key by hand
    (issue #64) and neither throttled nor rotated it.
    """
    headers = _HEADERS

    for delay in _RETRY_DELAYS:
        if delay:
            time.sleep(delay)
        try:
            r = requests.get(url, params=params, headers=headers, timeout=20)
            if r.status_code == 404:
                return None, True
            # other 4xx (except rate-limit 429) are definitive — retrying won't help
            if 400 <= r.status_code < 500 and r.status_code != 429:
                log.warning("doi_verify GET %s: HTTP %d (not retried)", url, r.status_code)
                return None, False
            r.raise_for_status()
            time.sleep(CROSSREF_RATE_SEC)
            return r.json(), False
        except Exception as exc:
            log.warning("doi_verify GET %s failed: %s", url, exc)
    return None, False


def _to_year(year) -> Optional[int]:
    try:
        return int(float(str(year)))
    except (ValueError, TypeError):
        return None


def _crossref_year(msg: dict) -> Optional[int]:
    """Extract published year from a CrossRef API message.

    Priority: published-print → published → published-online → issued → created.
    'created' is the DOI registration date — used only as last resort because
    publishers sometimes register DOIs months before the print date.
    """
    for k in ("published-print", "published", "published-online", "issued"):
        parts = (msg.get(k) or {}).get("date-parts") or []
        if parts and parts[0] and parts[0][0]:
            return int(parts[0][0])
    parts = (msg.get("created") or {}).get("date-parts") or []
    if parts and parts[0] and parts[0][0]:
        return int(parts[0][0])
    return None


def _surname(author: str) -> str:
    """Best-effort surname from 'Surname', 'Surname, First' or 'First Surname'."""
    author = (author or "").strip()
    if not author:
        return ""
    if "," in author:
        return author.split(",")[0].strip()
    return author.split()[-1]


def _fetch_via_content_negotiation(doi: str) -> "tuple[Optional[dict], bool]":
    """GET https://doi.org/{doi} with CSL-JSON content negotiation.

    Resolves DOIs registered with any registrar (publisher-direct, DataCite
    variants not yet indexed by OpenAlex).

    Returns (metadata, answered):
      (meta, True)  — the DOI resolves, here is what it points to
      (None, True)  — doi.org answered that it does not resolve (a real absence)
      (None, False) — doi.org never answered; absence is NOT established

    The second flag is the whole point: "no metadata" and "no answer" are the same
    None, and fetch_doi_metadata caches the first as `registered: false` forever.
    """
    headers = {**_HEADERS, "Accept": "application/vnd.citationstyles.csl+json"}
    for delay in _RETRY_DELAYS:
        if delay:
            time.sleep(delay)
        try:
            r = requests.get(f"https://doi.org/{doi}", headers=headers, timeout=20,
                             allow_redirects=True)
            if r.status_code == 404:
                return None, True
            if 400 <= r.status_code < 500 and r.status_code != 429:
                return None, False
            r.raise_for_status()
            csl = r.json()
            title = csl.get("title") or ""
            year = None
            for k in ("published-print", "issued", "published-online", "created"):
                parts = (csl.get(k) or {}).get("date-parts") or []
                if parts and parts[0] and parts[0][0]:
                    year = int(parts[0][0])
                    break
            authors = csl.get("author") or []
            first_surname = (authors[0].get("family") or "") if authors else ""
            return {"registered": True, "title": title,
                    "first_author_surname": first_surname,
                    "year": year, "type": str(csl.get("type") or ""),
                    "source": "content_negotiation"}, True
        except Exception as exc:
            log.warning("doi_verify content-negotiation %s failed: %s", doi, exc)
    return None, False


def fetch_doi_metadata(doi: str) -> Optional[dict]:
    """Return the metadata *doi* currently points to, or None on api_error.

    Shape: {"registered": bool, "title": str, "first_author_surname": str,
            "year": int|None, "type": str, "source": "crossref"|"openalex"}

    "type" is the registry's work type ("journal-article", "dataset", "peer-review",
    ...) and is "" when the source does not report one; see non_article_type().
    """
    doi = clean_doi(doi)
    # v3: the MEANING of a cached `registered: false` changed. Until the outage fix
    # below, CrossRef's 404 alone was enough to record "not registered" even when
    # OpenAlex or doi.org never answered, so entries written by that code can assert
    # an absence no source ever established. The answer may genuinely differ now, so
    # this is a key change and not a declared equivalence: old entries miss and are
    # recomputed under the safe logic. (v2 was the pre-"type" schema bump.)
    key = cache_key(doi + "_doimeta_v3")
    cached = read_cache(DOI_VERIFY_CACHE_DIR, key)
    if cached is not None:
        return cached

    data, not_found = _get_json(f"https://api.crossref.org/works/{doi}",
                                {"mailto": RESEARCHER_EMAIL})
    if data:
        msg     = data.get("message") or {}
        titles  = msg.get("title") or []
        authors = msg.get("author") or []
        meta = {
            "registered": True,
            "title": titles[0] if titles else "",
            "first_author_surname": (authors[0].get("family", "") if authors else ""),
            "year": _crossref_year(msg),
            "type": str(msg.get("type") or ""),
            "source": "crossref",
        }
        write_cache(DOI_VERIFY_CACHE_DIR, key, meta)
        return meta

    # CrossRef 404 does NOT mean unregistered — DataCite DOIs (Zenodo, OSF,
    # figshare) 404 on CrossRef but are indexed by OpenAlex. Hard failures
    # also fall through to OpenAlex.
    oa_data = _oa_get("https://api.openalex.org/works",
                      {"filter": f"doi:{doi}",
                       "select": "title,authorships,publication_year,type",
                       "mailto": RESEARCHER_EMAIL})
    data = oa_data
    if data and data.get("results"):
        w = data["results"][0]
        first = ((w.get("authorships") or [{}])[0].get("author") or {}).get("display_name", "")
        meta = {
            "registered": True,
            "title": w.get("title") or "",
            "first_author_surname": _surname(first),
            "year": w.get("publication_year"),
            "type": str(w.get("type") or ""),
            "source": "openalex",
        }
        write_cache(DOI_VERIFY_CACHE_DIR, key, meta)
        return meta

    if not_found:
        # CrossRef 404 + absent OpenAlex: try DOI content negotiation — resolves
        # ANY registrar (publisher-direct, DataCite variants not yet in OpenAlex).
        meta, cn_answered = _fetch_via_content_negotiation(doi)
        if meta:
            write_cache(DOI_VERIFY_CACHE_DIR, key, meta)
            return meta
        # "Not registered" is a claim about all three registries at once, so it may
        # only be cached when all three ANSWERED. CrossRef's 404 alone was being
        # taken as the answer while OpenAlex was merely unreachable (oa_data None) —
        # one outage then wrote `registered: false` for a perfectly good DOI, and
        # every later run read the outage back instead of re-asking. An unproven
        # absence is an api_error: uncached, and retried next time.
        if oa_data is None or not cn_answered:
            log.warning("doi_verify: %s is absent from CrossRef but %s did not "
                        "answer — not recording it as unregistered", doi,
                        "OpenAlex" if oa_data is None else "doi.org")
            return None
        meta = {"registered": False, "title": "", "first_author_surname": "",
                "year": None, "type": "", "source": "crossref"}
        write_cache(DOI_VERIFY_CACHE_DIR, key, meta)
        return meta

    # Both unavailable: cannot distinguish wrong-DOI from outage → api_error,
    # not cached.
    return None


def metadata_matches(meta: Optional[dict], title: str, author: str, year) -> bool:
    """True when *meta* plausibly describes (title, year). Author is not
    required here — surname formats vary too much across registries; the
    title Jaccard + year window carries the decision."""
    if not meta or not meta.get("registered") or not (title or "").strip():
        return False
    if jaccard_similarity(meta.get("title", ""), title) < VERIFY_TITLE_JACCARD:
        return False
    y_meta, y = meta.get("year"), _to_year(year)
    if y_meta and y and abs(y_meta - y) > YEAR_TOLERANCE:
        return False
    return True


def _score_hit(hit_title: str, hit_year, hit_surnames: list[str],
               title: str, author: str, year) -> bool:
    """Strict acceptance: a wrong auto-correction is worse than a flag."""
    if jaccard_similarity(hit_title, title) < RESOLVE_TITLE_JACCARD:
        return False
    y_hit, y = _to_year(hit_year), _to_year(year)
    if y_hit and y and abs(y_hit - y) > YEAR_TOLERANCE:
        return False
    surname = _surname(author)
    if surname and hit_surnames and not author_matches(surname, hit_surnames):
        return False
    return True


def _sanitize_search(text: str) -> str:
    """OpenAlex's search param returns HTTP 400 for some punctuation (e.g. '?').
    Keep word characters, spaces and hyphens; collapse whitespace."""
    return re.sub(r"\s+", " ", re.sub(r"[^\w\s\-]", " ", text)).strip()


def resolve_doi_by_metadata(title: str, author: str, year,
                            exclude_doi: str = "",
                            exclude_title: str = "",
                            title_only_gap: bool = False) -> Optional[dict]:
    """Find the DOI for (title, author, year) via CrossRef bibliographic
    search + OpenAlex search.

    exclude_doi: the replication's own DOI — replication titles often echo the
    original's title, so the search can return the replication paper itself.
    Prefix variants (e.g. .supp supplementary DOIs) are excluded too.

    exclude_title: the replication's own title. A preprint replication's
    published version has a different DOI but echoes title_r — hits whose
    title matches exclude_title better than *title* are rejected.

    title_only_gap: last-resort tier for rows whose author/year were inherited
    from a wrong DOI. Accepts the best title match ignoring author/year, but
    only when it clearly dominates the runner-up (≥ TITLE_ONLY_JACCARD and
    ≥ TITLE_ONLY_GAP × second-best).

    Returns {"doi", "title", "year", "openalex_id", "source"} on a hit, None when
    both sources answered and neither had a match, and UNVERIFIABLE (a falsy dict)
    when a source that could have held the match never answered. "doi" is "" when
    the work exists only in OpenAlex without a DOI.

    The third case exists because it was previously the second: a partial outage —
    CrossRef down, OpenAlex reachable but holding nothing — produced an empty hit
    list, which was cached as {"found": false} and read by verify_and_correct as
    "no replacement exists", which writes `mismatch`, which DELETES the row's doi_o.
    An outage must never do that.
    """
    title = (title or "").strip()
    if not title:
        return None
    exclude = clean_doi(exclude_doi or "")
    exclude_title = (exclude_title or "").strip()
    # v2: the MEANING of a cached `{"found": false}` changed. The pre-UNVERIFIABLE
    # code cached exactly the poisoned case — CrossRef down, OpenAlex answering
    # empty — as a definitive "no match exists", which verify_and_correct turns into
    # `mismatch` and DELETES the row's doi_o. Reading such an entry back would skip
    # the UNVERIFIABLE path entirely, so the key changes rather than being declared
    # equivalent: the answer provably may differ. Old entries miss and are recomputed.
    key = cache_key(f"{title}|{author}|{year}|{exclude}|{exclude_title}"
                    f"|{int(title_only_gap)}_doisearch_v2")
    cached = read_cache(DOI_VERIFY_CACHE_DIR, key)
    if cached is not None:
        # Under the v2 key a `found: false` was written only when BOTH sources
        # answered, so it is a real absence.
        return cached if cached.get("found") else None

    cr_data, _ = _get_json("https://api.crossref.org/works",
                           {"query.bibliographic": f"{title} {author}".strip(),
                            "rows": 10, "mailto": RESEARCHER_EMAIL})
    # A free-text `search` query, billed at 10× a filter query — _oa_get counts them
    # (search_query_count()) so the run can report what this ladder cost.
    oa_data = _oa_get("https://api.openalex.org/works",
                      {"search": _sanitize_search(title), "per-page": 10,
                       "select": "id,doi,title,authorships,publication_year",
                       "mailto": RESEARCHER_EMAIL})
    if cr_data is None and oa_data is None:
        return UNVERIFIABLE  # outage — don't cache

    hits: list[dict] = []
    for item in ((cr_data or {}).get("message") or {}).get("items", []):
        hits.append({
            "doi": clean_doi(item.get("DOI", "")),
            "title": (item.get("title") or [""])[0],
            "year": _crossref_year(item),
            "surnames": [a.get("family", "") for a in (item.get("author") or []) if a.get("family")],
            "openalex_id": "", "source": "crossref",
        })
    for w in (oa_data or {}).get("results", []):
        surnames = [_surname(((a.get("author") or {}).get("display_name", "")))
                    for a in (w.get("authorships") or [])]
        hits.append({
            "doi": clean_doi(w.get("doi") or ""),
            "title": w.get("title") or "",
            "year": w.get("publication_year"),
            "surnames": [s for s in surnames if s],
            "openalex_id": w.get("id", ""), "source": "openalex",
        })
    def _is_excluded(h_doi: str) -> bool:
        # exact match or separator-delimited variant (10.x/y.supp, 10.x/y/v2)
        return bool(exclude) and (
            h_doi == exclude
            or h_doi.startswith(exclude + ".")
            or h_doi.startswith(exclude + "/")
        )

    hits = [h for h in hits
            if not _is_excluded(h["doi"]) and not _ERRATA_RE.match(h["title"])]
    if exclude_title:
        # reject hits that look more like the replication than the original
        hits = [h for h in hits
                if jaccard_similarity(h["title"], title)
                >= jaccard_similarity(h["title"], exclude_title)]
    # CrossRef and OpenAlex often return the same work — dedupe by DOI so a
    # duplicate cannot tie with itself in the dominance check below.
    seen: set = set()
    deduped: list[dict] = []
    for h in hits:
        ident = h["doi"] or h["openalex_id"] or id(h)
        if ident in seen:
            continue
        seen.add(ident)
        deduped.append(h)
    hits = deduped

    def _accept(h: dict) -> dict:
        hit = {"found": True, "doi": h["doi"], "title": h["title"], "year": h["year"],
               "openalex_id": h["openalex_id"], "source": h["source"]}
        write_cache(DOI_VERIFY_CACHE_DIR, key, hit)
        return hit

    # In title-only mode the strict pass is skipped: with author/year absent it
    # would degenerate to a 0.7 title match without the dominance requirement.
    if not title_only_gap:
        for h in hits:
            if _score_hit(h["title"], h["year"], h["surnames"], title, author, year):
                return _accept(h)

    if title_only_gap and hits:
        scored = sorted(hits, key=lambda h: jaccard_similarity(h["title"], title),
                        reverse=True)
        best   = jaccard_similarity(scored[0]["title"], title)
        second = jaccard_similarity(scored[1]["title"], title) if len(scored) > 1 else 0.0
        if best >= TITLE_ONLY_JACCARD and best >= second * TITLE_ONLY_GAP:
            return _accept(scored[0])

    # No hit — but "nothing matched" is only an answer if both sources gave one. A
    # search that reached one registry and not the other has not established that the
    # original is unfindable, and caching it would freeze the outage into the row.
    if cr_data is None or oa_data is None:
        log.warning("doi_verify: metadata search for %.60r found nothing, but %s did "
                    "not answer — reporting it as unverifiable rather than absent",
                    title, "CrossRef" if cr_data is None else "OpenAlex")
        return UNVERIFIABLE
    write_cache(DOI_VERIFY_CACHE_DIR, key, {"found": False})
    return None


def keeps_no_doi(new_status: str, prior_status: str, oa_work_id_o: str) -> bool:
    """True when a fresh verification result must not overwrite a recorded "no_doi".

    verify_and_correct only ever searches for DOIs, so an original that genuinely has
    none always comes back "not_found" — or, when the search could not be completed,
    "api_error". Letting either overwrite "no_doi" would turn a row whose identity is
    its OpenAlex work id into an audit_extracted blocker, in the api_error case
    because of one outage. Both _verify_row (run_extract) and audit_dois re-verify the
    same rows, so the rule lives here rather than in either caller.
    """
    return (new_status in ("not_found", "api_error") and prior_status == "no_doi"
            and bool(bare_work_id(oa_work_id_o)))


def verify_and_correct(doi_o, title_o, author_o, year_o,
                       exclude_doi: str = "",
                       exclude_title: str = "") -> dict:
    """Verify (and if needed correct) doi_o against the resolved metadata.

    exclude_doi: the replication's own doi_r, never a valid correction target.
    exclude_title: the replication's own title (title_r) — guards against the
    published version of a preprint replication being picked as the original.

    Returns {"doi_o_verification": <status>, "doi_o": <possibly new doi>,
             "evidence_note": <text to append to link_evidence, may be "">}.
    """
    doi   = clean_doi(doi_o or "")
    title = (title_o or "").strip()

    if not doi and not title:
        return {"doi_o_verification": "skipped", "doi_o": doi, "evidence_note": ""}

    if doi:
        meta = fetch_doi_metadata(doi)
        if meta is None:
            return {"doi_o_verification": "api_error", "doi_o": doi,
                    "evidence_note": "DOI verification failed: CrossRef and OpenAlex unavailable"}
        if metadata_matches(meta, title, author_o, year_o):
            return {"doi_o_verification": "verified", "doi_o": doi, "evidence_note": ""}

        # The three search tiers, strictest first. `unverifiable` records whether any
        # of them failed to get an answer: the tiers below still run (a later one may
        # find the original outright, which settles the matter), but if none does, the
        # row must not be told that no replacement exists.
        unverifiable = False
        repl = resolve_doi_by_metadata(title, author_o, year_o,
                                       exclude_doi=exclude_doi,
                                       exclude_title=exclude_title)
        unverifiable |= repl is UNVERIFIABLE
        if not repl and _to_year(year_o) and _surname(author_o or ""):
            # year_o is often inherited from the wrong DOI's metadata, so a
            # year-constrained search can miss the true original. Retry without
            # the year — but only when an author surname can anchor the match.
            repl = resolve_doi_by_metadata(title, author_o, None,
                                           exclude_doi=exclude_doi,
                                           exclude_title=exclude_title)
            unverifiable |= repl is UNVERIFIABLE
        if not repl:
            # author_o can be inherited from the wrong DOI too — final tier
            # matches on title alone but requires a clearly dominant hit.
            repl = resolve_doi_by_metadata(title, "", None,
                                           exclude_doi=exclude_doi,
                                           exclude_title=exclude_title,
                                           title_only_gap=True)
            unverifiable |= repl is UNVERIFIABLE
        if not repl and unverifiable:
            # No replacement found, and the search could not be completed. The two
            # verdicts below this point — `mismatch`, which deletes doi_o and sends
            # the row to quarantine, and `no_metadata` — both assert that we looked
            # and found nothing better. We did not look. Keep the DOI and report the
            # failure the way every other field reports one.
            return {"doi_o_verification": "api_error", "doi_o": doi,
                    "evidence_note": ("DOI verification incomplete: the metadata "
                                      "search could not be completed (CrossRef or "
                                      "OpenAlex unavailable)")}
        if repl and clean_doi(repl.get("doi", "")) == doi:
            # the search re-found the very same DOI — the DOI is right, the
            # row's year_o/author_o metadata is what disagreed
            return {"doi_o_verification": "verified", "doi_o": doi,
                    "evidence_note": (f"DOI confirmed by metadata search; row year_o/"
                                      f"author_o disagree with DOI metadata "
                                      f"({meta.get('year')}, \"{meta.get('first_author_surname')}\")")}
        if repl and repl.get("doi"):
            reason = "pointed to a different work" if meta["registered"] else "is not registered"
            return {"doi_o_verification": "corrected", "doi_o": repl["doi"],
                    "evidence_note": (f"DOI corrected: {doi} {reason}; "
                                      f"replaced with {repl['doi']} (\"{repl['title']}\")")}
        if repl:
            return {"doi_o_verification": "no_doi", "doi_o": "",
                    "evidence_note": (f"DOI removed: {doi} did not match; original exists "
                                      f"without a DOI ({repl.get('openalex_id', '')})")}
        if meta["registered"]:
            return {"doi_o_verification": "mismatch", "doi_o": doi,
                    "evidence_note": (f"DOI mismatch: {doi} points to "
                                      f"\"{meta['title']}\" ({meta['year']}), not \"{title}\"")}
        return {"doi_o_verification": "no_metadata", "doi_o": doi,
                "evidence_note": f"DOI {doi} is not registered and no replacement was found"}

    # Blank DOI, title available — try to fill it.
    repl = resolve_doi_by_metadata(title, author_o, year_o, exclude_doi=exclude_doi,
                                   exclude_title=exclude_title)
    if repl is UNVERIFIABLE:
        # "not_found" below is a finding about the original — audit_extracted and
        # the validation import both read it as one. An unanswered search is not one.
        return {"doi_o_verification": "api_error", "doi_o": "",
                "evidence_note": ("DOI search incomplete: CrossRef or OpenAlex "
                                  "unavailable")}
    if repl and repl.get("doi"):
        return {"doi_o_verification": "corrected", "doi_o": repl["doi"],
                "evidence_note": f"DOI filled from metadata search: {repl['doi']}"}
    if repl:
        return {"doi_o_verification": "no_doi", "doi_o": "",
                "evidence_note": (f"Original has no registered DOI "
                                  f"({repl.get('openalex_id', '')})")}
    return {"doi_o_verification": "not_found", "doi_o": "",
            "evidence_note": "No DOI found for resolved title/author"}
