"""Derive alias entries that collapse the OpenAlex works naming one OSF guid (issue #200).

OpenAlex mints several work records for one OSF object — the registration, the
preprint, the published article and a handful of re-harvests of the same page — and
nothing in the pipeline derives identity from the OSF guid. Each work is therefore
screened, extracted and shipped on its own, so one study reaches the validation import
as several rows: 117 OSF records reached by more than one work id account for 296 of
the 2,602 rows of `data/extracted.csv`.

The maintainer's ruling is **same OSF guid ⇒ same record**, and the canonical work of a
group is, in order:

  1. a published article — a DOI whose registrant is outside OSF's own family;
  2. the OSF registration DOI (`10.17605`);
  3. another OSF-family DOI — a preprint server such as PsyArXiv (`10.31234`);
  4. the record identified only by an `osf.io` URL.

Tie-break within a tier by the lowest work id: the oldest OpenAlex record, the one most
likely to carry citations and complete metadata.

Tier 3 is this script's reading of the brief's three tiers. OSF mints DOIs on 28
registrants in this pool (`OSF_OWN_PREFIXES`), all of which encode the guid in the DOI
itself, and a preprint DOI is a weaker identity than the registration DOI but a stronger
one than a bare URL, so the preprint family sits between them rather than beside the
published article.

Identity is derived over the same identifiers Stage 3 reads — the row's DOI and its
`url_r` (`open_access.oa_url` or the primary location's landing page) — through
`osf_registration_guid()`. **That function, never a regex:** a hand-rolled copy of an
older pattern returned the path segment in FRONT of the guid and produced a 55-member
`download` group. It also strips a version suffix, so `d3x9p_v1` … `_v4` are one guid;
the versions of one preprint are one record.

This is deliberately WIDER than `osf_identifier()` in `search/fetch_abstracts.py`, which
refuses DOI-bearing rows so an abstract backfill cannot overwrite an article's abstract
with a registration template. That guard is about TEXT; this is about identity. Neither
is the other's predicate.

## What is refused, and why

A wrong merge fuses two distinct studies and nothing downstream says so. 10,163 of the
10,820 groups agree on a normalised title and are merged unexamined; the 657 that differ
were adjudicated against the OSF API (75 groups read, issue #200), and that reading gave
the mechanical rule below. Three classes are held back, in this order, each group
counted once:

  1. **oversized** — more than `MAX_GROUP_SIZE` members. The current maximum is 12
     (Many Labs 4), which is plausible for one OSF page; dozens is the shape a
     mis-parsed guid makes. Raise it with `--max-group-size` once a bigger group has
     been read.
  2. **unanchored title disagreement** — the group's titles differ, and the member
     holding the odd title out is not DOI-anchored on the guid: its guid came from a
     URL, which OpenAlex can mis-attribute. That is the one hazard the adjudication
     found real. `v2bfn` dragged in the sibling OSF component `2kbq4` through a stale
     secondary location, and two external-DOI cases were a MetaROR editorial assessment
     and a Blog of Trial and Error commentary sitting beside the preprint they discuss —
     all three would have become the surviving record, because the canonical rule prefers
     a non-OSF DOI. A member whose own DOI encodes `osf.io/<guid>` names the guid itself
     and cannot be a sibling mis-attribution, so a differing title there is a retitle.
  3. **alias-conflict** — the group cannot be merged into the existing
     `filter/spec/aliases.json` without contradicting it or building a chain.

**The SCORE label rename is exempt from rule 2** (`is_score_label_rename()`, and
`SCORE_RENAME_GUIDS` for the ones whose title moved a little further). Thirty-nine groups
carry titles differing in the phrase that encodes the replication type, and every one of
them has an OSF `edit_title` log from a single 2024-01-10 batch rename: `Data Analytic` →
`Secondary Data`, `Direct` → `New Data`. One object retitled in place, not two attempts
in one container — SCORE gives every attempt its own guid. Only those two label pairs
ever occur in the pool.

Rule 2 is conservative on purpose: many of the 405 groups it excludes are genuine
retitles that stay as separate works, which is the status quo rather than a regression.
Recovering one is a manual job and needs the network, so it is not in this script: read
the guid's OSF title history (the node's current title plus each `edit_title` log's
`title_original`/`title_new`) and merge the group when every distinct title in it appears
there. A preprint guid answers 404 on the node logs endpoint, which is "not verified",
not "merge".

The output is a FRAGMENT, written where `--out` says. It is never written into
`filter/spec/aliases.json`: the maintainer merges it after review, and the provenance of
the batch (which pool, which rule) goes in the commit message, because JSON has no
comments. Merging changes `alias_release`, which is an input to the routing release id,
so a run of `.venv/bin/python -m filter.engine route` has to follow it.

    .venv/bin/python -m analysis.build_osf_aliases --out /tmp/osf_aliases.json
"""

import argparse
import collections
import html
import json
import re
import sys
from pathlib import Path
from typing import Iterable, Optional

from filter.engine.workids import load_aliases, work_id
from shared.config import SNAPSHOT_POOL_DIR
from shared.pdf_sources import osf_registration_guid
from shared.utils import (OSF_OWN_PREFIXES, OSF_REGISTRATION_PREFIX,  # noqa: F401
                          clean_doi)

SPEC_DIR = Path(__file__).resolve().parent.parent / "filter" / "spec"
ALIASES = SPEC_DIR / "aliases.json"

# The largest group merged without a human reading it first. Measured maximum: 12.
MAX_GROUP_SIZE = 12

# The registrant sets live in shared/utils.py, beside `osf_type()`, which needs the
# same facts to say whether a row is a preprint or a project. Imported rather than
# repeated: two copies of a 28-registrant list is exactly the thing that drifts.
# `is_osf_family_doi()` below is still not the whole predicate — it also accepts a DOI
# whose suffix encodes the guid, so a server OSF adds later needs no edit in either file.

# The SCORE replication-type labels, normalised, as the 2024-01-10 batch rename left
# them: each set is one label written two ways over time. Only these two pairs occur in
# the pool, and a group whose titles differ only inside one pair is one retitled node.
SCORE_LABEL_PAIRS = (
    ("data analytic replication", "secondary data replication"),
    ("direct replication", "new data replication"),
)

# The 39 groups of that rename, each confirmed against its node's `edit_title` log. The
# list is here as well as the predicate because a title may have been edited alongside
# the label: `4rxgz` went from "Data Analytic Replication (ML)" to "Secondary Data
# Replication", which the pair test alone does not recognise.
SCORE_RENAME_GUIDS = {
    "2px95", "2zre3", "3czus", "4589g", "4rxgz", "5vgjw", "6zjdh", "8f5by", "8uex5",
    "9beuv", "a7h9n", "ba26p", "cbknq", "cqxyh", "da26y", "dy52c", "fjbvp", "fypm4",
    "gdv4x", "h6wg9", "hpgvj", "jxd7b", "kbm46", "ky2wm", "md5pu", "mqvyf", "n2dhg",
    "nmzbh", "nv6a3", "pr5jm", "rkfq5", "uscaj", "uzmtn", "xr8gz", "xtq68", "y5kbe",
    "ytuk9", "z6mkt", "zd2uq",
}

_EXCLUSION_REASONS = ("oversized", "unanchored_title_disagreement", "alias_conflict")


# ---------------------------------------------------------------------------
# Identity
# ---------------------------------------------------------------------------


def title_fingerprint(title: str) -> str:
    """*title* reduced to what a comparison should treat as the same record's.

    Case, punctuation and whitespace differ between two harvests of one OSF page, and
    entities arrive escaped a different number of times in each — up to three, which is
    why the unescaping repeats rather than running once.
    """
    text = title or ""
    for _ in range(3):
        text = html.unescape(text)
    return re.sub(r"[^a-z0-9]+", " ", text.lower()).strip()


def is_osf_family_doi(doi: str) -> bool:
    """Whether *doi* is one OSF minted — a registration or one of its preprint servers."""
    doi = (doi or "").strip().lower()
    if not doi:
        return False
    return doi.split("/")[0] in OSF_OWN_PREFIXES or bool(osf_registration_guid(doi))


def _tier(work: dict) -> int:
    """The canonical rank of *work*: lower wins. See the module docstring."""
    doi = (work.get("doi") or "").strip().lower()
    if not doi:
        return 3
    if not is_osf_family_doi(doi):
        return 0
    return 1 if doi.startswith(OSF_REGISTRATION_PREFIX + "/") else 2


def canonical(works: list[dict]) -> dict:
    """The work of *works* the others alias to."""
    return sorted(works, key=lambda w: (_tier(w), w["work"]))[0]


# ---------------------------------------------------------------------------
# Refusals
# ---------------------------------------------------------------------------


def is_score_label_rename(works: list[dict]) -> bool:
    """Whether the group's only title difference is one SCORE replication-type label.

    Proven merges: all 39 such groups in the pool carry an OSF `edit_title` log from the
    2024-01-10 vocabulary rename. Collapsing each pair to one marker and asking whether
    one title remains is the whole test — a group differing anywhere else keeps two.
    """
    prints = {title_fingerprint(w.get("title", "")) for w in works}
    if len(prints) < 2:
        return False
    for index, pair in enumerate(SCORE_LABEL_PAIRS):
        prints = {_replace_any(p, pair, f"score_label_{index}") for p in prints}
    return len(prints) == 1


def _replace_any(text: str, labels: tuple[str, ...], marker: str) -> str:
    for label in labels:
        text = text.replace(label, marker)
    return text


def _is_doi_anchored(work: dict, guid: str) -> bool:
    """Whether the work's own DOI names *guid* — `10.31234/osf.io/<guid>`, version or not.

    A DOI-anchored member cannot have been mis-attributed: the guid is in its identifier
    rather than in a location URL OpenAlex attached to it.
    """
    return osf_registration_guid(work.get("doi") or "") == guid


def _unanchored_disagreement(guid: str, works: list[dict]) -> bool:
    """Whether a member holding a title no other member shares is not DOI-anchored.

    The signature of an OpenAlex mis-location: a sibling OSF component, or a commentary
    on the preprint, pulled into the group by a URL that names the guid while the record
    is a different document.
    """
    counts = collections.Counter(title_fingerprint(w.get("title", "")) for w in works)
    if len(counts) < 2:
        return False
    return any(counts[title_fingerprint(w.get("title", ""))] == 1
               and not _is_doi_anchored(w, guid) for w in works)


def exclusion_reason(guid: str, works: list[dict], *,
                     max_group_size: int = MAX_GROUP_SIZE) -> Optional[str]:
    """Why this group must not be merged, or None. Order is fixed so counts sum."""
    if len(works) > max_group_size:
        return "oversized"
    if (_unanchored_disagreement(guid, works)
            and guid not in SCORE_RENAME_GUIDS and not is_score_label_rename(works)):
        return "unanchored_title_disagreement"
    return None


# ---------------------------------------------------------------------------
# The alias fragment
# ---------------------------------------------------------------------------


def build_aliases(groups: dict[str, list[dict]],
                  existing: dict[int, int]) -> tuple[dict[int, int], dict[str, str]]:
    """`(new aliases, conflicting guid → reason)` for already-screened *groups*.

    A group is dropped whole when merging it would contradict the existing map or build
    a chain, because `resolve()` follows exactly one hop and a half-merged group leaves
    two ids for one work — the thing the file exists to prevent. Four shapes:
    our key already aliased elsewhere; our canonical already an alias key; our key
    already someone else's canonical; and an entry already present, which is not a
    conflict and is simply not re-emitted.
    """
    existing_values = set(existing.values())
    aliases: dict[int, int] = {}
    conflicts: dict[str, str] = {}
    for guid, works in groups.items():
        target = canonical(works)["work"]
        entries = {w["work"]: target for w in works if w["work"] != target}
        reason = ""
        if target in existing:
            reason = f"canonical W{target} is already an alias of W{existing[target]}"
        for old in entries:
            if old in existing and existing[old] != target:
                reason = (f"W{old} is already an alias of W{existing[old]}, "
                          f"not of W{target}")
            elif old in existing_values:
                reason = f"W{old} is already the canonical of another group"
        if reason:
            conflicts[guid] = reason
            continue
        aliases.update({old: new for old, new in entries.items()
                        if existing.get(old) != new})
    _assert_invariants(aliases, existing)
    return aliases, conflicts


def _assert_invariants(aliases: dict[int, int], existing: dict[int, int]) -> None:
    """The three things the file must never say, checked over existing ∪ new."""
    merged = {**existing, **aliases}
    self_alias = {k for k, v in merged.items() if k == v}
    if self_alias:
        raise AssertionError(f"self-alias: {sorted(self_alias)[:5]}")
    chained = set(merged.values()) & set(merged)
    if chained:
        raise AssertionError(f"alias chain: {sorted(chained)[:5]}")
    if any(not isinstance(k, int) or not isinstance(v, int) for k, v in merged.items()):
        raise AssertionError("work ids must be ints")


def fragment(aliases: dict[int, int]) -> dict:
    """The alias file's own shape — `W`-prefixed string ids, as `aliases.json` writes them.

    The format is copied from the file rather than from the brief: `alias_release()`
    canonicalises layout but not spelling, so `"W123"` and `"123"` are two releases.
    """
    return {"version": 1,
            "aliases": dict(sorted((f"W{old}", f"W{new}")
                                   for old, new in aliases.items()))}


# ---------------------------------------------------------------------------
# The pool
# ---------------------------------------------------------------------------


def scan_pool(pool_dir: Path) -> dict[str, list[dict]]:
    """Every OSF guid the pool names, with the works naming it.

    One DuckDB pass over the parquet reading four columns; the guid is derived in
    Python because `osf_registration_guid()` is the only extractor allowed to derive it.
    The identifiers are the ones a pool row is rebuilt into — the DOI, and `url_r`
    (`open_access.oa_url` or the primary location's landing page) — so this scan sees an
    OSF record exactly where Stage 3 does.
    """
    import duckdb

    rows = duckdb.connect().execute(f"""
        select id,
               coalesce(doi, '') as doi,
               coalesce(display_name, title, '') as title,
               coalesce(primary_location, '') as location,
               coalesce(open_access, '') as open_access
        from read_parquet('{pool_dir}/*.parquet')
        where lower(coalesce(doi, '')) like '%osf.io%'
           or lower(coalesce(primary_location, '')) like '%osf.io%'
           or lower(coalesce(open_access, '')) like '%osf.io%'
    """).fetchall()

    grouped: dict[str, dict[int, dict]] = collections.defaultdict(dict)
    for work, doi, title, location, open_access in rows:
        doi = clean_doi(doi)
        guid = osf_registration_guid(doi) or osf_registration_guid(
            _json_field(open_access, "oa_url") or _json_field(location, "landing_page_url"))
        if not guid:
            continue
        wid = work_id(work)
        grouped[guid][wid] = {"work": wid, "doi": doi, "title": title}
    return {guid: list(works.values()) for guid, works in grouped.items()}


def _json_field(blob: str, key: str) -> str:
    """*key* of a JSON-string pool column, or "" — an unparseable blob names nothing."""
    if not blob:
        return ""
    try:
        return str((json.loads(blob) or {}).get(key) or "")
    except (ValueError, AttributeError):
        return ""


# ---------------------------------------------------------------------------
# Report
# ---------------------------------------------------------------------------


def _report(scanned: dict[str, list[dict]], groups: dict[str, list[dict]],
            merged: dict[str, list[dict]], excluded: dict[str, str],
            aliases: dict[int, int], samples: int = 10) -> None:
    surplus = sum(len(w) - 1 for w in groups.values())
    agreeing = sum(1 for w in groups.values()
                   if len({title_fingerprint(x.get("title", "")) for x in w}) == 1)
    reasons = collections.Counter(excluded.values())
    print(f"{sum(len(w) for w in scanned.values()):,} pool work(s) name an OSF guid, "
          f"over {len(scanned):,} distinct guid(s)")
    print(f"{len(groups):,} guid(s) are held by more than one work, "
          f"{sum(len(w) for w in groups.values()):,} work(s), {surplus:,} of them surplus")
    print(f"  title agreement: {agreeing:,} group(s) ({agreeing / max(len(groups), 1):.1%}) "
          f"share one normalised title; {len(groups) - agreeing:,} differ")
    retired = sum(len(w) - 1 for w in merged.values())
    print(f"\nmerged:   {len(merged):,} group(s), retiring {retired:,} surplus work(s) "
          f"of {surplus:,}")
    print(f"          {len(aliases):,} new alias entr(ies); "
          f"{retired - len(aliases):,} of the merges are already in the map")
    print(f"excluded: {len(excluded):,} group(s), "
          f"{sum(len(groups[g]) - 1 for g in excluded):,} surplus work(s) left standing")
    for reason in _EXCLUSION_REASONS:
        print(f"  {reason:34s} {reasons.get(reason, 0):,}")

    if merged:
        biggest = max(merged.items(), key=lambda kv: len(kv[1]))
        print(f"\nlargest merged group: {biggest[0]} with {len(biggest[1])} works")
    for guid, works_in in list(merged.items())[:samples]:
        chosen = canonical(works_in)
        print(f"\n  {guid}")
        for work in sorted(works_in, key=lambda w: w["work"]):
            mark = "canonical" if work["work"] == chosen["work"] else "alias    "
            print(f"    {mark}  W{work['work']}  {work['doi'] or '(no doi)':32s}  "
                  f"{(work.get('title') or '')[:60]}")


def _print_conflicts(conflicts: dict[str, str], limit: int = 5) -> None:
    for guid, reason in list(conflicts.items())[:limit]:
        print(f"  {guid}: {reason}")
    if len(conflicts) > limit:
        print(f"  … {len(conflicts) - limit} more")


def main(argv: "Optional[Iterable[str]]" = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, required=True,
                        help="where the alias fragment is written — never aliases.json, "
                             "which the maintainer merges by hand after review")
    parser.add_argument("--pool-dir", type=Path, default=SNAPSHOT_POOL_DIR)
    parser.add_argument("--aliases", type=Path, default=ALIASES,
                        help="the existing alias map, checked for contradictions")
    parser.add_argument("--max-group-size", type=int, default=MAX_GROUP_SIZE,
                        help=f"refuse groups larger than this (default {MAX_GROUP_SIZE})")
    args = parser.parse_args(list(argv) if argv is not None else None)

    if not args.pool_dir.exists():
        print(f"no pool at {args.pool_dir}", file=sys.stderr)
        return 2
    if args.out.resolve() == ALIASES.resolve():
        print("--out may not be filter/spec/aliases.json: this script writes a fragment "
              "for review, and the merge is the maintainer's", file=sys.stderr)
        return 2

    scanned = scan_pool(args.pool_dir)
    groups = {guid: works for guid, works in scanned.items() if len(works) > 1}
    excluded: dict[str, str] = {}
    keep: dict[str, list[dict]] = {}
    for guid, works in groups.items():
        reason = exclusion_reason(guid, works, max_group_size=args.max_group_size)
        if reason:
            excluded[guid] = reason
        else:
            keep[guid] = works

    existing = load_aliases(args.aliases)
    aliases, conflicts = build_aliases(keep, existing)
    for guid in conflicts:
        keep.pop(guid, None)
        excluded[guid] = "alias_conflict"

    _report(scanned, groups, keep, excluded, aliases)
    if conflicts:
        print(f"\nalias conflicts ({len(conflicts)}):")
        _print_conflicts(conflicts)

    args.out.write_text(json.dumps(fragment(aliases), indent=2) + "\n", encoding="utf-8")
    print(f"\nwrote {len(aliases):,} alias entr(ies) -> {args.out}")
    print(f"existing map: {len(existing):,} entr(ies); merged file would hold "
          f"{len(existing) + len(aliases):,}")
    print("Merge it into filter/spec/aliases.json after review, then run "
          "`.venv/bin/python -m filter.engine route` — the alias file is an input to "
          "the routing release id.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
