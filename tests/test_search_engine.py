"""
Smoke tests for the YAML-spec discovery engine. Live API calls are NOT made —
adapters are exercised via injected sessions in higher-level tests.
"""

from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

from search.engine.runner import SPEC_FRESHNESS_DAYS, check_spec_freshness


def test_stale_spec_warns_not_raises():
    """#48: a spec older than SPEC_FRESHNESS_DAYS must warn, not raise — a hard raise
    silently empties the engine source (engine_source swallows the failed run)."""
    stale = SimpleNamespace(
        id="openalex",
        verified_at=datetime.now(timezone.utc) - timedelta(days=SPEC_FRESHNESS_DAYS + 5),
    )
    # No logger: must return normally (previously raised RuntimeError).
    check_spec_freshness({"openalex": stale}, log=None)

    warnings: list = []
    logger = SimpleNamespace(warning=lambda *a, **k: warnings.append(a))
    check_spec_freshness({"openalex": stale}, log=logger)
    assert warnings, "stale spec should emit a warning when a logger is supplied"


def test_fresh_spec_no_warning():
    fresh = SimpleNamespace(id="openalex", verified_at=datetime.now(timezone.utc))
    warnings: list = []
    logger = SimpleNamespace(warning=lambda *a, **k: warnings.append(a))
    check_spec_freshness({"openalex": fresh}, log=logger)
    assert not warnings


from search.engine.candidate_normalizer import (
    merge_candidates,
    normalize_candidate,
    normalize_doi,
)
from search.engine.candidate_ranker import compute_search_score, load_ranking_weights
from search.engine.exclusion_filter import (
    apply_exclusions,
    load_exclusion_patterns,
)
from search.engine.keyword_expander import (
    expand_all,
    expand_wildcard,
    load_spec_keywords,
)
from search.engine.types import (
    MatchedKeyword,
    NormalizedCandidate,
    RawCandidate,
)

SPEC_DIR = Path(__file__).parent.parent / "search" / "spec"


def test_spec_loads():
    specs = load_spec_keywords(SPEC_DIR)
    assert len(specs) >= 15, "spec should contain at least the 17 ported keywords"
    ids = {s.id for s in specs}
    assert "REP_OF" in ids
    assert "REP_QUALIFIED" in ids


def test_keyword_expansion_dedups():
    specs = load_spec_keywords(SPEC_DIR)
    expanded = expand_all(specs, [])
    perms = [e.permutation.lower() for e in expanded]
    assert len(set(perms)) == len(perms), "expanded keywords must be unique by phrase"


def test_user_wildcards():
    specs = []
    expanded = expand_all(specs, ['"exact phrase"', "(close|systematic) replication", "replicat*"])
    perms = [e.permutation for e in expanded]
    assert "exact phrase" in perms
    assert "close replication" in perms
    assert "systematic replication" in perms
    assert "replication" in perms        # via STEM_DICT
    assert "replications" in perms


def test_optional_char_wildcard():
    out = expand_wildcard("pre-?registered")
    assert "pre-registered" in out
    assert "preregistered" in out


def test_exclusion_dna():
    patterns = load_exclusion_patterns(SPEC_DIR)
    assert apply_exclusions("DNA replication in eukaryotes", patterns).excluded
    assert not apply_exclusions("conceptual replication of Smith 2020", patterns).excluded


def test_normalize_doi():
    assert normalize_doi("https://doi.org/10.1037/abc") == "10.1037/abc"
    assert normalize_doi("DOI: 10.1037/ABC ") == "10.1037/abc"
    assert normalize_doi("10.1037/abc/") == "10.1037/abc"


def _raw(doi: str, *, title=None, abstract=None, kw_id="REP_OF", field="title", perm="replication of", source="openalex"):
    return RawCandidate(
        source=source,
        doi=doi,
        title=title,
        abstract=abstract,
        matched_keyword=MatchedKeyword(id=kw_id, field=field, permutation=perm),
    )


def test_merge_keeps_richer():
    a = normalize_candidate(_raw("10.1/x", title="A title"))
    b = normalize_candidate(_raw("10.1/x", abstract="An abstract", source="crossref",
                                 kw_id="DIRECT_REP", field="abstract", perm="direct replication"))
    merged = merge_candidates(a, b)
    assert merged.title == "A title"
    assert merged.abstract == "An abstract"
    assert {m.id for m in merged.matched_keywords} == {"REP_OF", "DIRECT_REP"}


def test_search_score_rules():
    weights = load_ranking_weights(SPEC_DIR)
    only_abstract = NormalizedCandidate(
        source="openalex", doi="10/x",
        matched_keywords=[MatchedKeyword(id="A", field="abstract", permutation="x")],
    )
    only_title = NormalizedCandidate(
        source="openalex", doi="10/x",
        matched_keywords=[MatchedKeyword(id="A", field="title", permutation="x")],
    )
    multi = NormalizedCandidate(
        source="openalex", doi="10/x",
        matched_keywords=[
            MatchedKeyword(id="A", field="title", permutation="x"),
            MatchedKeyword(id="B", field="abstract", permutation="y"),
        ],
    )
    s_abs = compute_search_score(only_abstract, {"openalex"}, weights)
    s_title = compute_search_score(only_title, {"openalex"}, weights)
    s_multi = compute_search_score(multi, {"openalex", "crossref"}, weights)
    assert 0 < s_abs < s_title <= weights.cap
    assert s_multi == weights.cap   # title + multi-keyword + multi-source pinned
