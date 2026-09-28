"""shared/doi_registry.py — what a DOI's registry names, and the issue #210 verdict rule."""

from shared import doi_registry


def _crossref(title, subtitle=(), year=2012, original=()):
    return {"message": {"title": [title], "subtitle": list(subtitle),
                        "original-title": list(original), "short-title": [],
                        "container-title": ["J"], "type": "journal-article",
                        "issued": {"date-parts": [[year]]}}}


def test_the_verdict_rule(monkeypatch):
    """A twin shares no token with its DOI's title; a dropped subtitle, a title the
    registry holds in the original language, or a translation in another script
    within a year are not mismatches."""
    records = {
        "10.1/twin": _crossref("Complementary and Alternative Medicine in Diabetes Care"),
        "10.1/sub": _crossref("Job embeddedness", subtitle=["evidence from India"]),
        "10.1/orig": _crossref("Memory and aging", original=["Gedächtnis und Altern"]),
        "10.1/ja": _crossref("A replication of the mirror effect", year=2012),
    }
    monkeypatch.setattr(doi_registry, "_get", lambda url, params, headers: (
        records[url.rsplit("/works/", 1)[1]], True))
    check = doi_registry.check
    twin = check("10.1/twin", "The effects of Verb Network Strengthening Treatment", 2012)
    assert twin["verdict"] == "mismatch" and twin["similarity"] == 0.0
    assert check("10.1/sub", "Job Embeddedness: Evidence from India", 2012)["verdict"] == "match"
    assert check("10.1/orig", "Gedächtnis und Altern", 2012)["verdict"] == "match"
    assert check("10.1/ja", "鏡映効果の追試", 2013)["verdict"] == "translation_suspect"
    assert check("10.1/ja", "鏡映効果の追試", 2020)["verdict"] == "mismatch"
    assert check("", "anything")["verdict"] == "no_doi"


def test_a_failure_is_never_cached_and_never_a_mismatch(monkeypatch):
    calls = []

    def failing(url, params, headers):
        calls.append(url)
        return None, False

    monkeypatch.setattr(doi_registry, "_get", failing)
    assert doi_registry.check("10.1/down", "Title")["verdict"] == "unanswered"
    assert doi_registry.cached_only("10.1/down") is None

    # Both routes answering 404 IS an answer, and is cached.
    monkeypatch.setattr(doi_registry, "_get", lambda url, params, headers: (None, True))
    assert doi_registry.check("10.1/gone", "Title")["verdict"] == "unregistered"
    assert doi_registry.cached_only("10.1/gone")["registered"] is False
    assert doi_registry.check("10.1/gone", "Title", network=False)["verdict"] == "unregistered"


def test_whose_paper_an_abstract_describes():
    """The side whose title words the abstract covers more; a tie keeps the abstract.
    Real cases from the issue #210 audit (W22986889, W4415949651)."""
    names = doi_registry.abstract_names_registry
    reg = ["Complementary and Alternative Medicine in Diabetes Care"]
    cam = "People with diabetes use complementary and alternative medicine in their care."
    assert names(cam, "Verb Network Strengthening Treatment in aphasia: replication",
                 reg)["names_registry"]
    own = "The CRepair wrapper improved structural self-repair across three LLM families."
    assert not names(own, "CRepair Wrapper Improves Structural Self-Repair",
                     ["Can LLMs Correct Themselves?"])["names_registry"]
    assert not names("Unrelated words entirely.", "Aphasia treatment", reg)["names_registry"]
