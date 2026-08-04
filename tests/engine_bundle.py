"""A tiny synthetic spec bundle and pool, for testing engine MECHANICS.

The engine's mechanics — precedence resolution, the shadow contract, the
`no_text` downgrade, the store, the export, the diagnostics delta — are
properties of the code, not of the shipped rules. Testing them against
`filter/spec/` made every deliberate policy change (a precedence move, a rule
flipped to shadow because its evidence turned out to be unsound) break a dozen
tests that were not about policy at all.

So mechanics are tested here, against seven made-up rules and eight made-up
rows, chosen so that between them they exercise every pile, both evidence kinds
(regex and field), a shadow rule that matches but may never win, a precedence
conflict, a row nothing claims, and a row with no abstract in each of the two
piles that treat it differently. What the SHIPPED rules do is asserted in exactly
two other places: their policy in `tests/test_engine_spec.py` (the policy table),
their patterns in `tests/test_engine_147_rules.py` and the backend-equality tests
of `tests/test_engine_route.py`.
"""

import json
import shutil
from pathlib import Path

from filter.engine.spec import FilterSpec, load_specs, validate_spec

REAL_SPEC_DIR = Path(__file__).resolve().parent.parent / "filter" / "spec"

# Policy files the engine reads from a bundle directory: the pile→status mapping
# an export writes under, and the work-id aliases. Both are copied from the real
# bundle — they are policy the mechanics consume, not rules under test.
_COPIED = ("conventions.json", "aliases.json")

_TRUSTED = [{"level": "trusted", "rationale": "synthetic fixture"}]

SPECS: list[dict] = [
    # Two live discards: one on a field, one on a DOI prefix. The field rule sits
    # above the phrase rules so a row matching both resolves by precedence.
    {"id": "syn-dataset", "description": "a deposited dataset is not a study",
     "match": {"fields": {"type": ["dataset"]}},
     "pile": "discard", "precedence": 950, "measured": _TRUSTED},
    {"id": "syn-deposit", "description": "a repository DOI prefix is a deposit",
     "match": {"doi_prefix": ["10.7910"]},
     "pile": "discard", "precedence": 940, "measured": _TRUSTED},
    # A shadow discard that matches plenty: it must be evaluated and recorded and
    # must never claim a pile, however high its precedence.
    {"id": "syn-shadow", "description": "an unmeasured guess, kept in draft",
     "match": {"text_regex": r"(?i)\bbees\b"},
     "pile": "discard", "precedence": 800, "shadow": True,
     "measured": [{"level": "heuristic", "rationale": "synthetic fixture"}]},
    {"id": "syn-cite", "description": "a phrase plus a named citation",
     "match": {"all_of": [{"text_regex": r"(?i)direct replication"},
                          {"text_regex": r"(?i)\bet\s+al\."}]},
     "pile": "screen_expensive", "precedence": 350},
    {"id": "syn-reproduction", "description": "reproduction vocabulary",
     "match": {"abstract_regex": r"(?i)reproduced the original"},
     "pile": "screen_cheap", "vocabulary": "reproduction", "precedence": 262},
    {"id": "syn-replication", "description": "replication vocabulary",
     "match": {"text_regex": r"(?i)direct replication"},
     "pile": "screen_cheap", "vocabulary": "replication", "precedence": 260},
    {"id": "syn-concept", "description": "a topical concept, no vocabulary",
     "match": {"fields": {"concept_ids": ["C12590798"]}},
     "pile": "screen_cheap", "precedence": 220},
]

_CITE = "as reported by Smith et al. (2019)"


def pool_row(work: int, doi=None, title="A study of bees", abstract="Bees are nice.",
             type_="article", year=2024, concepts=()) -> dict:
    """One survivor-pool row (`search.snapshot_scan._POOL_SCHEMA`)."""
    return {
        "id": f"https://openalex.org/W{work}",
        "doi": doi,
        "title": title,
        "display_name": title,
        "publication_year": year,
        "type": type_,
        "authorships": json.dumps([{"author": {"display_name": "A. Author"}}]),
        "primary_location": json.dumps({"source": {"display_name": "J. Repl."},
                                        "landing_page_url": "https://example.org/1"}),
        "open_access": json.dumps({"oa_url": None}),
        "concepts": json.dumps([{"id": f"https://openalex.org/{c}"} for c in concepts]),
        "abstract_text": abstract,
        "hit_token_title": True,
        "hit_token_abstract": False,
        "hit_concept": bool(concepts),
    }


# Eight rows over four piles. The comment on each says what it is there to prove.
POOL_ROWS = [
    # discard by precedence, over two lower rules (one of them shadow).
    pool_row(1, type_="dataset", title="Replication Data for: Bees", year=2020,
             abstract="A direct replication of the anchoring effect."),
    # screen_expensive.
    pool_row(2, title="A direct replication of the Smith effect", year=2021,
             abstract=f"We report a direct replication of the anchoring effect, {_CITE}."),
    # screen_cheap, vocabulary reproduction.
    pool_row(3, title="Computational check", year=2022,
             abstract="We reproduced the original results of the published analysis."),
    # screen_cheap, vocabulary replication.
    pool_row(4, title="A direct replication", year=2023,
             abstract="We report a direct replication of the anchoring effect."),
    # screen_cheap by field evidence, no vocabulary.
    pool_row(5, title="Anchoring in context", abstract="A study of judgement.",
             year=2024, concepts=("C12590798",)),
    # matched by the shadow rule ONLY: nothing live claims it, so it is pending.
    pool_row(6, title="A study of bees", year=2025),
    # a screening route with no abstract: downgraded to pending/no_text.
    pool_row(7, title="A direct replication of the Smith effect", abstract=None,
             year=2026),
    # a discard with no abstract: NOT downgraded — a discard reads no abstract.
    pool_row(8, doi="https://doi.org/10.7910/DVN/ABC", year=2020,
             title="Replication Data for: Wasps", abstract=None),
]

# What the bundle above does to the pool above, as (pile, winning rule id).
EXPECTED_ROUTING = {
    1: ("discard", "syn-dataset"),
    2: ("screen_expensive", "syn-cite"),
    3: ("screen_cheap", "syn-reproduction"),
    4: ("screen_cheap", "syn-replication"),
    5: ("screen_cheap", "syn-concept"),
    6: ("pending", ""),
    7: ("pending", "syn-replication"),
    8: ("discard", "syn-deposit"),
}


def write_bundle(spec_dir: Path, extra: list[dict] = ()) -> Path:
    """Write `SPECS` (plus *extra*) and the copied policy files into *spec_dir*."""
    spec_dir = Path(spec_dir)
    spec_dir.mkdir(parents=True, exist_ok=True)
    for raw in list(SPECS) + list(extra):
        (spec_dir / f"{raw['id']}.json").write_text(json.dumps(raw, indent=1),
                                                    encoding="utf-8")
    for name in _COPIED:
        shutil.copy(REAL_SPEC_DIR / name, spec_dir / name)
    return spec_dir


def specs() -> list[FilterSpec]:
    """`SPECS` as validated FilterSpecs, without touching the filesystem.

    Validated because a fixture bundle that the real loader would reject is not a
    fixture for anything — the mechanics under test only ever run on legal specs.
    """
    errors = [f"{raw.get('id')}: {e}" for raw in SPECS for e in validate_spec(raw)]
    if errors:
        raise ValueError("invalid synthetic bundle:\n  " + "\n  ".join(errors))
    return sorted((FilterSpec.from_dict(raw) for raw in SPECS),
                  key=lambda s: (-s.precedence, s.id))


def real_specs() -> list[FilterSpec]:
    return load_specs(REAL_SPEC_DIR)
