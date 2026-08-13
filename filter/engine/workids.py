"""Work identity: the int64 OpenAlex id, and the alias map that canonicalises it.

Everything the engine keys — routing rows, evaluations, claims — is keyed by the
alias-resolved id, so one article does not appear twice under two ids across
releases. `filter/spec/aliases.json` is flat (`old_id → canonical_id`) and
`resolve()` follows exactly one hop: a chain would mean the file needs
re-flattening, not that the resolver needs a loop.

The map covers the merges OpenAlex performed and the ones it did not: a repository
deposit and the publisher's record of one article are two work ids upstream.
`analysis/doi_duplicates.py` derives the second kind from the pool and adds them to
the file, and `analysis/build_osf_aliases.py` derives the same-OSF-guid merges;
this module only reads the file.
"""

import json
import re
from pathlib import Path

from filter.engine.spec import canonical_json_digest

_ID_RE = re.compile(r"^(?:https?://openalex\.org/)?[Ww]?(\d+)$")


def work_id(openalex_id: str) -> int:
    """`https://openalex.org/W123`, `W123` or `123` as the int64 work id."""
    match = _ID_RE.match((openalex_id or "").strip())
    if not match:
        raise ValueError(f"not an OpenAlex work id: {openalex_id!r}")
    return int(match.group(1))


def load_aliases(path: Path) -> dict[int, int]:
    """The alias map from *path*; an absent file is an empty map, not an error."""
    if not path.exists():
        return {}
    raw = json.loads(path.read_text(encoding="utf-8"))
    return {work_id(str(old)): work_id(str(new))
            for old, new in (raw.get("aliases") or {}).items()}


def alias_release(path: Path) -> str:
    """sha256 of the alias file's JSON content — a routing release input.

    Canonical over the parsed JSON for the same reason the spec bundle is
    (`canonical_json_digest()` in `spec.py`): reformatting the file must not
    invalidate every stored release. Note this canonicalises LAYOUT only — the
    same alias written `"W123"` in one revision and `"123"` in the next is two
    different releases, even though `load_aliases()` reads them identically.
    """
    return canonical_json_digest(path).hex()


def resolve(work_id: int, aliases: dict[int, int]) -> int:
    """*work_id* mapped through *aliases* (one hop; unknown ids pass through)."""
    return aliases.get(work_id, work_id)
