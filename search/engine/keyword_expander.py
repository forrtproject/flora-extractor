"""
Keyword expander — turns user wildcards and the YAML spec's keyword list into
a flat list of (keyword id, phrase variant) rows that adapters OR-bundle into
one search query per source.

Wildcard syntax (matches the TS engine's tab UI / CLI config):
    replicat*                        → trailing-stem expansion via STEM_DICT
    pre-?registered                  → optional preceding char (zero or one)
    (close|high-powered) replication → alternation groups
    "exact phrase"                   → quoted literal, no expansion
"""

import re
from pathlib import Path

import yaml

from .types import ExpandedKeyword, KeywordSpec

# Hand-curated stems used by trailing-* wildcards. Adding a new stem is the
# recommended way to extend wildcard support; introducing fuller morphology
# (e.g. via a stemmer) would compromise the deterministic guarantee that
# "same input → same expansion" across the TS engine and this Python port.
STEM_DICT: dict[str, list[str]] = {
    "replicat": [
        "replicate", "replicated", "replicates",
        "replicating", "replication", "replications",
    ],
    "reproduc": [
        "reproduce", "reproduced", "reproduces",
        "reproducing", "reproducible", "reproducibility",
    ],
}

_ALT_RE = re.compile(r"\(([^()]+)\)")
_OPT_RE = re.compile(r"(.)\?")
_QUOTED_RE = re.compile(r'^"(.+)"$')
_STAR_RE = re.compile(r"^(.*?)(\w+)\*(.*)$")


def expand_wildcard(input_str: str) -> list[str]:
    """Expand a single user-input string into the literal phrases it stands for.

    Order of operations: quoted literal → alternation → optional char → trailing star.
    """
    trimmed = input_str.strip()
    if not trimmed:
        return []

    quoted = _QUOTED_RE.match(trimmed)
    if quoted:
        return [quoted.group(1)]

    alt = _ALT_RE.search(trimmed)
    if alt:
        opts = [s.strip() for s in alt.group(1).split("|") if s.strip()]
        out: list[str] = []
        seen: set[str] = set()
        for opt in opts:
            for v in expand_wildcard(_ALT_RE.sub(opt, trimmed, count=1)):
                if v not in seen:
                    seen.add(v)
                    out.append(v)
        return out

    opt = _OPT_RE.search(trimmed)
    if opt:
        ch = opt.group(1)
        idx = opt.start()
        without = trimmed[:idx] + trimmed[idx + 2:]
        with_ch = trimmed[:idx] + ch + trimmed[idx + 2:]
        out2: list[str] = []
        seen2: set[str] = set()
        for v in expand_wildcard(without) + expand_wildcard(with_ch):
            if v not in seen2:
                seen2.add(v)
                out2.append(v)
        return out2

    if "*" in trimmed:
        m = _STAR_RE.match(trimmed)
        if m:
            prefix, stem, suffix = m.group(1), m.group(2), m.group(3)
            stems = STEM_DICT.get(stem.lower(), [stem])
            return [f"{prefix}{s}{suffix}" for s in stems]

    return [trimmed]


def expand_spec_keyword(spec: KeywordSpec) -> list[ExpandedKeyword]:
    """Expand a single YAML spec entry into ExpandedKeyword rows."""
    out: list[ExpandedKeyword] = []
    if spec.template and spec.qualifiers:
        for q in spec.qualifiers:
            out.append(ExpandedKeyword(
                id=spec.id,
                permutation=spec.template.replace("{qualifier}", q),
                weight=spec.weight,
                fields=spec.fields,
            ))
        return out
    for perm in spec.permutations or []:
        out.append(ExpandedKeyword(
            id=spec.id,
            permutation=perm,
            weight=spec.weight,
            fields=spec.fields,
        ))
    return out


def expand_user_input(raw_keywords: list[str]) -> list[ExpandedKeyword]:
    """Expand user-supplied keywords (with wildcards) into ExpandedKeyword rows.

    Each user keyword gets a synthetic id so its hits remain attributable.
    """
    out: list[ExpandedKeyword] = []
    for raw in raw_keywords:
        variants = expand_wildcard(raw)
        if not variants:
            continue
        slug = re.sub(r"[^a-z0-9]", "_", raw, flags=re.I).upper()[:32]
        wid = f"USER_{slug}"
        for v in variants:
            out.append(ExpandedKeyword(
                id=wid,
                permutation=v,
                weight=0.85,
                fields=["title", "abstract"],
            ))
    return out


def expand_all(
    spec_keywords: list[KeywordSpec],
    user_keywords: list[str],
) -> list[ExpandedKeyword]:
    """Combine spec + user keywords; dedup by phrase (case-insensitive).

    Spec entries are added first, so when a user types a phrase the spec already
    covers, the spec entry wins (its id and weight are kept).
    """
    seen: set[str] = set()
    out: list[ExpandedKeyword] = []

    def consider(k: ExpandedKeyword) -> None:
        key = k.permutation.lower()
        if key in seen:
            return
        seen.add(key)
        out.append(k)

    for s in spec_keywords:
        for e in expand_spec_keyword(s):
            consider(e)
    for e in expand_user_input(user_keywords):
        consider(e)
    return out


def load_spec_keywords(spec_dir: Path | str) -> list[KeywordSpec]:
    """Parse search-keywords.yaml from a spec directory."""
    spec_dir = Path(spec_dir)
    with (spec_dir / "search-keywords.yaml").open("r", encoding="utf-8") as f:
        doc = yaml.safe_load(f)

    out: list[KeywordSpec] = []
    for k in doc.get("keywords", []):
        out.append(KeywordSpec(
            id=k["id"],
            weight=float(k["weight"]),
            fields=list(k.get("fields", ["title", "abstract"])),
            phrase=k.get("phrase"),
            template=k.get("template"),
            qualifiers=list(k["qualifiers"]) if k.get("qualifiers") else None,
            permutations=list(k["permutations"]) if k.get("permutations") else None,
            notes=k.get("notes"),
        ))
    return out
