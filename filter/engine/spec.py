"""Filter spec format v1 — load, validate, hash.

One JSON file per filter under `filter/spec/`; the file's content hash is its
version, so `bundle_hash()` is whitespace-sensitive on purpose: an edit that
changes nothing but formatting still mints a new routing release, which is
cheaper than trying to define "meaningful" change.

Regexes must be RE2-safe. Both backends (Python `re` and pyarrow, milestone 1
`backends.py`) have to agree row for row, and pyarrow's matcher is RE2: a
lookaround or backreference would evaluate in one backend and raise in the
other. `re2_safe()` is what makes that a load-time error rather than a
production divergence.
"""

import hashlib
import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

SPEC_VERSION = 1

# Files in `filter/spec/` that are policy or lookup data, not filters.
NON_SPEC_FILES = frozenset({"conventions.json", "aliases.json", "holdout.json"})

PILES = ("discard", "screen_expensive", "screen_cheap", "needs_human")
VOCABULARIES = ("replication", "reproduction")

# `heuristic` is deliberately absent: it records a guess, and a guess may not
# discard autonomously (see CONVENTIONS.md). A heuristic-only discard must be
# shadow.
AUTONOMOUS_LEVELS = ("human", "downstream", "trusted")

_SPEC_KEYS = frozenset({"id", "description", "match", "pile", "vocabulary",
                        "precedence", "shadow", "measured"})
_MATCH_KEYS = frozenset({"doi_prefix", "doi_regex", "title_regex",
                         "abstract_regex", "text_regex", "fields",
                         "abstract_missing", "any_of", "all_of", "none_of"})
_NESTED_KEYS = ("any_of", "all_of", "none_of")
_REGEX_KEYS = ("doi_regex", "title_regex", "abstract_regex", "text_regex")
_FIELD_KEYS = frozenset({"type", "publication_year", "concept_ids"})
_MEASURED_KEYS = frozenset({"level", "precision", "n", "sample", "date",
                            "owner", "rationale"})

# Loader-only extension. `filter/phrase_detection.py` still evaluates these two
# patterns with Python `re`, where the original lookaround semantics survive; the
# vectorized engine evaluates the RE2 decomposition next to it. Legal ONLY on a
# match block that is actually decomposed (any_of/all_of/none_of non-empty), and
# only at the top level of `match` — a flat spec that needs a `pyre_regex` is a
# spec whose RE2 rewrite was never done.
PYRE_REGEX_KEY = "pyre_regex"


@dataclass(frozen=True)
class MatchBlock:
    """One match object: every present condition ANDs with the others."""

    doi_prefix: tuple[str, ...] = ()
    doi_regex: Optional[str] = None
    title_regex: Optional[str] = None
    abstract_regex: Optional[str] = None
    text_regex: Optional[str] = None
    # Pairs rather than a dict so the frozen dataclass stays hashable.
    fields: tuple[tuple[str, tuple[Any, ...]], ...] = ()
    abstract_missing: Optional[bool] = None
    any_of: tuple["MatchBlock", ...] = ()
    all_of: tuple["MatchBlock", ...] = ()
    none_of: tuple["MatchBlock", ...] = ()
    pyre_regex: Optional[str] = None

    @classmethod
    def from_dict(cls, raw: dict) -> "MatchBlock":
        return cls(
            doi_prefix=tuple(raw.get("doi_prefix") or ()),
            doi_regex=raw.get("doi_regex"),
            title_regex=raw.get("title_regex"),
            abstract_regex=raw.get("abstract_regex"),
            text_regex=raw.get("text_regex"),
            fields=tuple((k, tuple(v)) for k, v in (raw.get("fields") or {}).items()),
            abstract_missing=raw.get("abstract_missing"),
            any_of=tuple(cls.from_dict(m) for m in (raw.get("any_of") or ())),
            all_of=tuple(cls.from_dict(m) for m in (raw.get("all_of") or ())),
            none_of=tuple(cls.from_dict(m) for m in (raw.get("none_of") or ())),
            pyre_regex=raw.get(PYRE_REGEX_KEY),
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

    @classmethod
    def from_dict(cls, raw: dict) -> "FilterSpec":
        return cls(
            id=raw["id"],
            description=raw.get("description", ""),
            match=MatchBlock.from_dict(raw.get("match") or {}),
            pile=raw["pile"],
            precedence=int(raw["precedence"]),
            vocabulary=raw.get("vocabulary"),
            shadow=bool(raw.get("shadow", False)),
            measured=tuple(raw.get("measured") or ()),
        )


# ---------------------------------------------------------------------------
# RE2 safety
# ---------------------------------------------------------------------------

_BANNED_GROUPS = {
    "(?=": "lookahead",
    "(?!": "negative lookahead",
    "(?<=": "lookbehind",
    "(?<!": "negative lookbehind",
    "(?>": "atomic group",
    "(?(": "conditional",
    "(?P=": "backreference",
}


def re2_safe(pattern: str) -> bool:
    """True if *pattern* uses only constructs RE2 (and so pyarrow) implements.

    Rejects lookaround, backreferences, atomic groups, possessive quantifiers,
    conditionals and `\\G`. Ordinary syntax passes, including non-capturing
    groups, named groups and inline flags such as `(?i)`.
    """
    i, n = 0, len(pattern)
    in_class = False
    prev_was_quantifiable_close = False
    while i < n:
        ch = pattern[i]
        if ch == "\\":
            nxt = pattern[i + 1] if i + 1 < n else ""
            if not in_class and (nxt.isdigit() and nxt != "0"):
                return False            # \1..\9 backreference
            if nxt == "G":
                return False
            i += 2
            prev_was_quantifiable_close = not in_class
            continue
        if in_class:
            if ch == "]":
                in_class = False
                prev_was_quantifiable_close = True
            i += 1
            continue
        if ch == "[":
            in_class = True
            i += 1
            continue
        if ch == "(":
            for opener, _label in _BANNED_GROUPS.items():
                if pattern.startswith(opener, i):
                    return False
            # Skip the `(?` so the flag/type char is never read as a quantifier.
            i += 2 if pattern.startswith("(?", i) else 1
            prev_was_quantifiable_close = False
            continue
        if ch in "*+?}" and prev_was_quantifiable_close:
            if i + 1 < n and pattern[i + 1] == "+":
                return False            # possessive quantifier
            i += 1
            continue                    # stays quantifiable: `a*?`, `a{2,}?`
        prev_was_quantifiable_close = ch not in "|("
        i += 1
    return True


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


def _validate_match(raw: Any, path: str, top: bool, errors: list[str]) -> None:
    if not isinstance(raw, dict):
        errors.append(f"{path}: match must be an object")
        return
    allowed = _MATCH_KEYS | ({PYRE_REGEX_KEY} if top else set())
    for key in sorted(set(raw) - allowed):
        errors.append(f"{path}: unknown key {key!r}")
    if PYRE_REGEX_KEY in raw:
        if not any(raw.get(k) for k in _NESTED_KEYS):
            errors.append(
                f"{path}: {PYRE_REGEX_KEY!r} is only valid on a decomposed match "
                "(any_of/all_of/none_of)")
        try:
            re.compile(raw[PYRE_REGEX_KEY])
        except (re.error, TypeError) as exc:
            errors.append(f"{path}.{PYRE_REGEX_KEY}: does not compile ({exc})")
    for key in _REGEX_KEYS:
        value = raw.get(key)
        if value is None:
            continue
        if not isinstance(value, str):
            errors.append(f"{path}.{key}: must be a string")
            continue
        if not re2_safe(value):
            errors.append(f"{path}.{key}: not RE2-safe: {value!r}")
        try:
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
            _validate_match(block, f"{path}.{key}[{idx}]", False, errors)


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


def _level_ok(level: str) -> bool:
    if level.startswith("llm:"):
        return bool(level[4:].strip())
    return level in ("human", "downstream", "heuristic", "trusted")


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

    _validate_match(raw.get("match"), f"{label}.match", True, errors)

    measured = raw.get("measured", [])
    _validate_measured(measured, label, errors)

    if pile == "discard" and shadow is not True:
        if not isinstance(measured, list) or not measured:
            errors.append(f"{label}: a discard spec needs a measured entry "
                          "or shadow: true")
        elif all(isinstance(e, dict) and e.get("level") == "heuristic"
                 for e in measured):
            errors.append(f"{label}: heuristic evidence may not discard "
                          "autonomously — measure it or set shadow: true")
    return errors


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


def bundle_hash(spec_dir: Path) -> str:
    """sha256 over the (filename, bytes) pairs of the bundle, order-independent."""
    digest = hashlib.sha256()
    for path in _spec_files(spec_dir):
        digest.update(path.name.encode("utf-8"))
        digest.update(b"\0")
        digest.update(hashlib.sha256(path.read_bytes()).digest())
    return digest.hexdigest()
