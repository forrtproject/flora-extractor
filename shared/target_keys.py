"""target_keys.py — one @key namespace over a paper's candidates and its references.

The target-identification prompt shows the model two lists that overlap: the
OpenAlex candidates pre-matched on author-year, and the paper's reference list. When
each list carried its own 1..N numbering, "reference 3" meant a different work
depending on which list the model was reading, a work present in both was offered
twice as if it were two studies, and a re-fetched reference list renumbered every
choice — so a replayed pick pointed at another paper.

Keys are BibTeX-style (@smith2009) over the deduplicated union of both lists, and a
returned key is only ever resolved against the key_map from the same call.
"""
import re

from .utils import clean_doi

_KEY_STRIP   = re.compile(r"[^a-z0-9]")
_TITLE_STRIP = re.compile(r"[^a-z0-9 ]")
_WS          = re.compile(r"\s+")


def _norm_title(title: str) -> str:
    return _WS.sub(" ", _TITLE_STRIP.sub(" ", str(title or "").lower())).strip()


def _surname(author: str) -> str:
    text = str(author or "").strip()
    if not text:
        return ""
    parts = text.split()
    head = text.split(",")[0] if "," in text else (parts[-1] if parts else "")
    return _KEY_STRIP.sub("", head.lower())


def _authors_of(record: dict) -> list[str]:
    authors = record.get("all_authors") or record.get("authors")
    if isinstance(authors, list):
        named = [str(a).strip() for a in authors if str(a).strip()]
        if named:
            return named
    # Raw OpenAlex works arrive with authorships rather than a flattened author list.
    named = [str((a.get("author") or {}).get("display_name", "")).strip()
             for a in (record.get("authorships") or [])]
    named = [a for a in named if a]
    if named:
        return named
    first = str(record.get("first_author") or "").strip()
    return [first] if first else []


def _year_of(record: dict) -> "int | None":
    for field in ("year", "publication_year"):
        try:
            year = int(record.get(field))
        except (TypeError, ValueError):
            continue
        if year:
            return year
    return None


def _normalise(record: dict, role: str) -> dict:
    authors = _authors_of(record)
    return {
        "doi":           clean_doi(str(record.get("doi") or "")),
        "title":         str(record.get("title") or "").strip(),
        "year":          _year_of(record),
        "authors":       authors,
        "first_author":  authors[0] if authors else "",
        "openalex_id":   str(record.get("openalex_id") or record.get("id") or ""),
        "in_candidates": role == "candidate",
        "in_references": role == "reference",
    }


def _merge(into: dict, new: dict) -> None:
    for field in ("doi", "title", "openalex_id", "first_author"):
        if not into[field] and new[field]:
            into[field] = new[field]
    if into["year"] is None:
        into["year"] = new["year"]
    if len(new["authors"]) > len(into["authors"]):
        into["authors"]      = new["authors"]
        into["first_author"] = new["authors"][0]
    into["in_candidates"] = into["in_candidates"] or new["in_candidates"]
    into["in_references"] = into["in_references"] or new["in_references"]


def _suffix(n: int) -> str:
    return chr(ord("a") + n) if n < 26 else str(n)


def assign_target_keys(candidates: list[dict],
                       references: list[dict]) -> tuple[list[dict], dict[str, dict]]:
    """Deduplicate candidates and references into one keyed namespace.

    Returns (entries in encounter order, key → entry). A work appearing on both
    sides is ONE entry carrying both roles, so the prompt never offers it twice.
    """
    entries:  list[dict]        = []
    by_doi:   dict[str, dict]   = {}
    by_title: dict[tuple, dict] = {}

    for record, role in ([(c, "candidate") for c in candidates or []]
                         + [(r, "reference") for r in references or []]):
        if not isinstance(record, dict):
            continue
        entry = _normalise(record, role)
        if not entry["title"] and not entry["doi"]:
            continue
        title_key = (_norm_title(entry["title"]), entry["year"])
        existing = by_doi.get(entry["doi"]) if entry["doi"] else None
        if existing is None and title_key[0]:
            candidate_match = by_title.get(title_key)
            # Title and year agreeing is only evidence of sameness while at least one
            # side is DOI-less. Two DIFFERENT registered DOIs are two records — a
            # correction and its original, a preprint and its version of record —
            # and merging them makes the first DOI answer for the second.
            if candidate_match is not None and not (
                    entry["doi"] and candidate_match["doi"]
                    and entry["doi"] != candidate_match["doi"]):
                existing = candidate_match
        if existing is not None:
            _merge(existing, entry)
            if existing["doi"]:
                by_doi.setdefault(existing["doi"], existing)
            continue
        entries.append(entry)
        if entry["doi"]:
            by_doi[entry["doi"]] = entry
        if title_key[0]:
            # First seen wins the title slot, so which record a later DOI-less
            # duplicate joins does not depend on list order beyond that.
            by_title.setdefault(title_key, entry)

    used:    dict[str, int]  = {}
    key_map: dict[str, dict] = {}
    for entry in entries:
        surname = _surname(entry["first_author"])
        year    = entry["year"]
        if surname and year:
            base = f"@{surname}{year}"
        elif surname:
            base = f"@{surname}"
        elif year:
            base = f"@anon{year}"
        else:
            base = "@anon"
        n = used.get(base, 0)
        used[base] = n + 1
        key = base if n == 0 else f"{base}{_suffix(n)}"
        entry["key"] = key
        key_map[key] = entry

    return entries, key_map
