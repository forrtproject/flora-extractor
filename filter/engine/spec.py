"""Filter spec format v1 — load, validate, hash.

One JSON file per filter under `filter/spec/`; the bundle's hash is its version.
The hash is taken over each file's PARSED JSON re-serialised canonically
(`canonical_json_digest()`), not over its bytes, so reindenting a spec or
reordering its keys does not mint a new routing release and invalidate every
stored one. A change to any value — including a regex, where every character is
significant and is preserved exactly by the reserialisation — still does.

Regexes must be ones RE2 can run. The engine has one evaluator, pyarrow compute
(`backends.py`), and pyarrow's matcher is RE2: a lookaround or backreference
raises there. `re2_error()` asks RE2 itself, at spec load, so an unrunnable
pattern is a named error against a named file instead of a crash partway through
a routing run.
"""

import hashlib
import json
import re
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Any, Optional

import pyarrow as pa
import pyarrow.compute as pc

SPEC_VERSION = 1

# Files in `filter/spec/` that are policy or lookup data, not filters.
NON_SPEC_FILES = frozenset({"conventions.json", "aliases.json", "holdout.json"})

# Policy files that are not filters but DO decide what a row ends up saying:
# conventions.json holds the pile→paper_type mapping the export writes, so a
# routing release that did not bind it could be exported under a different
# vocabulary policy than the one it was routed under. `aliases.json` is
# deliberately absent — it has its own release input (`alias_release`) — and
# `holdout.json` names an evaluation set, which changes no row's status.
POLICY_FILES = ("conventions.json",)

PILES = ("discard", "screen_expensive", "screen_cheap", "needs_human")
VOCABULARIES = ("replication", "reproduction")

# Evidence levels that may discard a row on their own. `heuristic` is
# deliberately absent: it records a guess, and a guess may not discard
# autonomously (see CONVENTIONS.md) — a heuristic-only discard must be shadow.
# `llm:<model>` counts as autonomous too, which `_is_autonomous()` adds.
AUTONOMOUS_LEVELS = ("human", "downstream", "trusted")

_SPEC_KEYS = frozenset({"id", "description", "match", "domain", "pile",
                        "vocabulary", "precedence", "shadow", "measured"})
_MATCH_KEYS = frozenset({"doi_prefix", "doi_regex", "title_regex",
                         "abstract_regex", "text_regex", "url_regex", "fields",
                         "abstract_missing", "any_of", "all_of", "none_of"})
_NESTED_KEYS = ("any_of", "all_of", "none_of")
_REGEX_KEYS = ("doi_regex", "title_regex", "abstract_regex", "text_regex",
               "url_regex")
_FIELD_KEYS = frozenset({"type", "publication_year", "concept_ids"})
_MEASURED_KEYS = frozenset({"level", "precision", "n", "sample", "date",
                            "owner", "rationale"})


@dataclass(frozen=True)
class MatchBlock:
    """One match object: every present condition ANDs with the others."""

    doi_prefix: tuple[str, ...] = ()
    doi_regex: Optional[str] = None
    title_regex: Optional[str] = None
    abstract_regex: Optional[str] = None
    text_regex: Optional[str] = None
    # Over the row's own URL, which the pool carries inside its `open_access` /
    # `primary_location` JSON rather than as a column (`backends.row_url()`).
    url_regex: Optional[str] = None
    # Pairs rather than a dict so the frozen dataclass stays hashable.
    fields: tuple[tuple[str, tuple[Any, ...]], ...] = ()
    abstract_missing: Optional[bool] = None
    any_of: tuple["MatchBlock", ...] = ()
    all_of: tuple["MatchBlock", ...] = ()
    none_of: tuple["MatchBlock", ...] = ()

    @classmethod
    def from_dict(cls, raw: dict) -> "MatchBlock":
        return cls(
            doi_prefix=tuple(raw.get("doi_prefix") or ()),
            doi_regex=raw.get("doi_regex"),
            title_regex=raw.get("title_regex"),
            abstract_regex=raw.get("abstract_regex"),
            text_regex=raw.get("text_regex"),
            url_regex=raw.get("url_regex"),
            fields=tuple((k, tuple(v)) for k, v in (raw.get("fields") or {}).items()),
            abstract_missing=raw.get("abstract_missing"),
            any_of=tuple(cls.from_dict(m) for m in (raw.get("any_of") or ())),
            all_of=tuple(cls.from_dict(m) for m in (raw.get("all_of") or ())),
            none_of=tuple(cls.from_dict(m) for m in (raw.get("none_of") or ())),
        )


@dataclass(frozen=True)
class FilterSpec:
    """One filter: a match, a destination pile, and the evidence for it."""

    id: str
    description: str
    match: MatchBlock
    pile: str
    precedence: int
    vocabulary: Optional[str] = None
    shadow: bool = False
    measured: tuple[dict, ...] = field(default_factory=tuple)
    # The population the rule claims to govern, when it declares one. Optional,
    # diagnostic, and it never gates the match — see `_validate_domain()`.
    domain: Optional[MatchBlock] = None

    @classmethod
    def from_dict(cls, raw: dict) -> "FilterSpec":
        return cls(
            id=raw["id"],
            description=raw.get("description", ""),
            match=MatchBlock.from_dict(raw.get("match") or {}),
            domain=(MatchBlock.from_dict(raw["domain"])
                    if raw.get("domain") else None),
            pile=raw["pile"],
            precedence=int(raw["precedence"]),
            vocabulary=raw.get("vocabulary"),
            shadow=bool(raw.get("shadow", False)),
            measured=tuple(raw.get("measured") or ()),
        )


# ---------------------------------------------------------------------------
# RE2 syntax
# ---------------------------------------------------------------------------

# One row is enough to make pyarrow build the RE2 program; a pattern RE2 refuses
# raises here rather than mid-route. Cached because load_specs() runs on every
# command and the bundle holds ~140 patterns.
_PROBE = pa.array([""])


@lru_cache(maxsize=None)
def re2_error(pattern: str) -> Optional[str]:
    """RE2's complaint about *pattern*, or None if RE2 can run it.

    Asked of RE2 itself through pyarrow, which is the engine that will run the
    pattern — a hand-written scanner of banned constructs (lookaround,
    backreferences, atomic groups, possessive quantifiers, `\\G`) could only
    reject what its author thought of, and RE2 rejects more than that. The
    engine's evaluator is pyarrow alone, so this is a syntax check, not a
    claim that two implementations agree.
    """
    try:
        pc.match_substring_regex(_PROBE, pattern)
    except pa.ArrowInvalid as exc:
        return str(exc).split("\n", 1)[0]
    return None


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


def _validate_match(raw: Any, path: str, errors: list[str]) -> None:
    if not isinstance(raw, dict):
        errors.append(f"{path}: match must be an object")
        return
    for key in sorted(set(raw) - _MATCH_KEYS):
        errors.append(f"{path}: unknown key {key!r}")
    for key in _REGEX_KEYS:
        value = raw.get(key)
        if value is None:
            continue
        if not isinstance(value, str):
            errors.append(f"{path}.{key}: must be a string")
            continue
        complaint = re2_error(value)
        if complaint:
            errors.append(f"{path}.{key}: RE2 cannot run it ({complaint}): {value!r}")
        try:
            # Not a second evaluator: `match_evidence()` asks `re` where an
            # already-matched pattern matched, and a pattern it cannot compile
            # would raise there instead of naming the spec that shipped it.
            re.compile(value)
        except re.error as exc:
            errors.append(f"{path}.{key}: does not compile ({exc}): {value!r}")
    prefixes = raw.get("doi_prefix")
    if prefixes is not None and (not isinstance(prefixes, list)
                                 or not all(isinstance(p, str) for p in prefixes)):
        errors.append(f"{path}.doi_prefix: must be a list of strings")
    fields = raw.get("fields")
    if fields is not None:
        if not isinstance(fields, dict):
            errors.append(f"{path}.fields: must be an object")
        else:
            for key, value in fields.items():
                if key not in _FIELD_KEYS:
                    errors.append(f"{path}.fields: unknown column {key!r}")
                if not isinstance(value, list) or not value:
                    errors.append(f"{path}.fields.{key}: must be a non-empty list")
    missing = raw.get("abstract_missing")
    if missing is not None and not isinstance(missing, bool):
        errors.append(f"{path}.abstract_missing: must be a boolean")
    for key in _NESTED_KEYS:
        nested = raw.get(key)
        if nested is None:
            continue
        if not isinstance(nested, list):
            errors.append(f"{path}.{key}: must be a list of match objects")
            continue
        for idx, block in enumerate(nested):
            _validate_match(block, f"{path}.{key}[{idx}]", errors)


def _validate_domain(raw: Any, label: str, errors: list[str]) -> None:
    """`domain` is a match object naming the population the rule claims to govern.

    Optional, and it does not gate the match: a domain that narrowed what the rule
    matched would change routing, and the field exists precisely to be compared
    AGAINST the match rather than merged into it. It is a cheap, data-only
    predicate over columns the pool always carries, so the comparison stays true
    when the text a rule reads is missing — which is the failure it was added for
    (2026-08-08, `osf-registration-protocol`).

    An empty object is refused: a domain of everything is not a claim, and it
    would report the whole pool as one rule's uncovered population.
    """
    if raw is None:
        return
    if not isinstance(raw, dict) or not raw:
        errors.append(f"{label}.domain: must be a non-empty match object naming the "
                      "population this rule claims to govern")
        return
    _validate_match(raw, f"{label}.domain", errors)


def _validate_measured(entries: Any, path: str, errors: list[str]) -> None:
    if not isinstance(entries, list):
        errors.append(f"{path}: measured must be a list")
        return
    for idx, entry in enumerate(entries):
        here = f"{path}.measured[{idx}]"
        if not isinstance(entry, dict):
            errors.append(f"{here}: must be an object")
            continue
        for key in sorted(set(entry) - _MEASURED_KEYS):
            errors.append(f"{here}: unknown key {key!r}")
        level = entry.get("level")
        if not isinstance(level, str) or not _level_ok(level):
            errors.append(
                f"{here}.level: must be one of human/downstream/heuristic/trusted "
                f"or 'llm:<model>', got {level!r}")
        if "precision" in entry:
            precision = entry["precision"]
            if not isinstance(precision, (int, float)) or isinstance(precision, bool) \
                    or not 0 < precision <= 1:
                errors.append(f"{here}.precision: must be in (0, 1], got {precision!r}")
        if not entry.get("rationale"):
            errors.append(f"{here}.rationale: required")


def _is_autonomous(level: str) -> bool:
    """True when *level* is evidence strong enough to discard without shadow."""
    return level in AUTONOMOUS_LEVELS or (
        level.startswith("llm:") and bool(level[4:].strip()))


def _level_ok(level: str) -> bool:
    return _is_autonomous(level) or level == "heuristic"


def validate_spec(raw: dict) -> list[str]:
    """Every problem with *raw* as a v1 spec, as human-readable strings."""
    errors: list[str] = []
    if not isinstance(raw, dict):
        return ["spec must be a JSON object"]

    spec_id = raw.get("id")
    label = spec_id if isinstance(spec_id, str) and spec_id else "<no id>"
    if not isinstance(spec_id, str) or not spec_id.strip():
        errors.append("id: required, non-empty string")
    for key in sorted(set(raw) - _SPEC_KEYS):
        errors.append(f"{label}: unknown key {key!r}")
    if not isinstance(raw.get("description"), str) or not raw.get("description"):
        errors.append(f"{label}.description: required, non-empty string")

    pile = raw.get("pile")
    if pile == "pending":
        errors.append(
            f"{label}.pile: 'pending' is never a spec target — the engine assigns "
            "it, specs route into discard/screen_expensive/screen_cheap/needs_human")
    elif pile not in PILES:
        errors.append(f"{label}.pile: must be one of {'/'.join(PILES)}, got {pile!r}")

    precedence = raw.get("precedence")
    if not isinstance(precedence, int) or isinstance(precedence, bool):
        errors.append(f"{label}.precedence: must be an int, got {precedence!r}")

    vocabulary = raw.get("vocabulary")
    if vocabulary is not None and vocabulary not in VOCABULARIES:
        errors.append(f"{label}.vocabulary: must be null/replication/reproduction, "
                      f"got {vocabulary!r}")

    shadow = raw.get("shadow", False)
    if not isinstance(shadow, bool):
        errors.append(f"{label}.shadow: must be a boolean, got {shadow!r}")

    _validate_match(raw.get("match"), f"{label}.match", errors)
    _validate_domain(raw.get("domain"), label, errors)

    measured = raw.get("measured", [])
    _validate_measured(measured, label, errors)

    if pile == "discard" and shadow is not True:
        if not isinstance(measured, list) or not measured:
            errors.append(f"{label}: a discard spec needs a measured entry "
                          "or shadow: true")
        elif not any(isinstance(e, dict) and isinstance(e.get("level"), str)
                     and _is_autonomous(e["level"]) for e in measured):
            errors.append(f"{label}: heuristic evidence may not discard "
                          "autonomously — measure it or set shadow: true")
        content_key = _content_key_used(raw.get("match"))
        if content_key and (raw.get("match") or {}).get("abstract_missing") is not False:
            errors.append(
                f"{label}: a live discard that reads the abstract ({content_key}) "
                "must carry \"abstract_missing\": false at the top level of its "
                "match — a row with no text has said nothing, and an empty string "
                "is not evidence for deleting the work")
    return errors


def _content_key_used(raw: Any) -> Optional[str]:
    """The first abstract-reading key anywhere in *raw*'s match tree, or None.

    `text_regex` counts: it matches over title + "\\n" + abstract
    (`backends.py`), so a rule written against "text" reads the abstract whether
    its author meant to or not.
    """
    if not isinstance(raw, dict):
        return None
    for key in ("abstract_regex", "text_regex"):
        if raw.get(key) is not None:
            return key
    for nested_key in _NESTED_KEYS:
        for block in raw.get(nested_key) or ():
            found = _content_key_used(block)
            if found:
                return found
    return None


# ---------------------------------------------------------------------------
# Loading and hashing
# ---------------------------------------------------------------------------


def _spec_files(spec_dir: Path) -> list[Path]:
    return sorted(p for p in spec_dir.glob("*.json") if p.name not in NON_SPEC_FILES)


def load_specs(spec_dir: Path) -> list[FilterSpec]:
    """Every validated spec in *spec_dir*, highest precedence first.

    Raises ValueError listing *all* errors across *all* files: a spec bundle is
    edited by hand, and fixing one error per run is a bad way to spend a review.
    """
    errors: list[str] = []
    specs: list[FilterSpec] = []
    seen: dict[str, str] = {}
    for path in _spec_files(spec_dir):
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            errors.append(f"{path.name}: invalid JSON: {exc}")
            continue
        file_errors = validate_spec(raw)
        errors.extend(f"{path.name}: {e}" for e in file_errors)
        if file_errors:
            continue
        if raw["id"] in seen:
            errors.append(f"{path.name}: duplicate id {raw['id']!r} "
                          f"(already defined in {seen[raw['id']]})")
            continue
        seen[raw["id"]] = path.name
        specs.append(FilterSpec.from_dict(raw))
    if errors:
        raise ValueError("invalid filter spec bundle:\n  " + "\n  ".join(errors))
    return sorted(specs, key=lambda s: (-s.precedence, s.id))


def canonical_json_digest(path: Path) -> bytes:
    """sha256 of *path*'s JSON content, canonically re-serialised.

    The one place a bundle input is turned into bytes to hash: `bundle_hash()`
    for the specs and policy files, `workids.alias_release()` for the alias map.
    Sorting the keys and dropping the layout means formatting a file — indent,
    key order, trailing newline — does not invalidate a stored release, while
    any change to a value still does. String values, regexes included, are
    reproduced character for character, so what a spec MATCHES is untouched.

    An absent file digests as empty, so a bundle directory in a test is still
    hashable. A file that is not valid JSON is an error naming the file: the
    hash cannot describe content it could not read.
    """
    if not path.exists():
        return hashlib.sha256(b"").digest()
    try:
        obj = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"{path.name}: invalid JSON, cannot hash it: {exc}") from exc
    payload = json.dumps(obj, sort_keys=True, separators=(",", ":"),
                         ensure_ascii=False)
    return hashlib.sha256(payload.encode("utf-8")).digest()


def bundle_hash(spec_dir: Path) -> str:
    """sha256 over the (filename, canonical JSON) pairs of the bundle.

    Files are hashed in name order, so the result does not depend on the order
    the directory lists them. Covers the spec files AND `POLICY_FILES`: the
    bundle is everything that decides what a row is routed to and what that pile
    is then called.
    """
    digest = hashlib.sha256()
    for path in _spec_files(spec_dir) + sorted(
            (spec_dir / name for name in POLICY_FILES), key=lambda p: p.name):
        digest.update(path.name.encode("utf-8"))
        digest.update(b"\0")
        digest.update(canonical_json_digest(path))
    return digest.hexdigest()
