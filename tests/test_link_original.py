"""Tests for citation-context extraction in extract/link_original.py."""
from extract.link_original import _extract_cit_contexts


class TestExtractCitContexts:
    def test_narrative_citation_detected(self):
        """The old local regex only matched fully-parenthetical citations like
        '(Antle, 2010)' and missed narrative citations like 'Kim et al. (2014)' —
        this was the confirmed root cause of the aepp.13320 wrong-original-link bug."""
        text = "In this paper, we replicate Kim et al. (2014) who study downside risk."
        results = _extract_cit_contexts(text)
        assert any(r["surnames"] == ["kim"] and r["year"] == 2014 for r in results)

    def test_parenthetical_citation_still_detected(self):
        text = "We compare our results to the partial moments model (Antle, 2010)."
        results = _extract_cit_contexts(text)
        assert any(r["surnames"] == ["antle"] and r["year"] == 2010 for r in results)

    def test_both_narrative_and_parenthetical_present(self):
        """Reconstructs the real aepp.13320 case: the true target is cited
        narratively, a secondary comparison is cited parenthetically — both must
        be extractable so the resolver can score and pick the right one."""
        text = (
            "we replicate Kim et al. (2014) who perform a quantile moments-based "
            "analysis. We compare to the partial moments model (Antle, 2010)."
        )
        results = _extract_cit_contexts(text)
        surnames_years = {(tuple(r["surnames"]), r["year"]) for r in results}
        assert (("kim",), 2014) in surnames_years
        assert (("antle",), 2010) in surnames_years

    def test_journal_hint_extracted_from_parenthetical(self):
        text = ("This builds on prior work (Antle, 2010, American Journal of "
                 "Agricultural Economics).")
        results = _extract_cit_contexts(text)
        match = next(r for r in results if r["surnames"] == ["antle"])
        assert "American Journal of Agricultural Economics" in match["journal"]

    def test_no_journal_when_absent(self):
        text = "We compare our results to the partial moments model (Antle, 2010)."
        results = _extract_cit_contexts(text)
        match = next(r for r in results if r["surnames"] == ["antle"])
        assert match["journal"] == ""

    def test_multi_author_surnames_all_preserved(self):
        text = "Jones and Smith (2015) found similar effects in a related domain."
        results = _extract_cit_contexts(text)
        match = next(r for r in results if r["year"] == 2015)
        assert set(match["surnames"]) == {"jones", "smith"}

    def test_no_citation_returns_empty(self):
        assert _extract_cit_contexts("No citations appear in this sentence at all.") == []
