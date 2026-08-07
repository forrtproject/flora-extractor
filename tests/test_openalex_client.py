"""
Tests for shared/openalex_client.py — candidate-matching logic.

All OpenAlex HTTP calls are mocked; no network access required.
Run: python -m pytest tests/test_openalex_client.py -v
"""
from unittest.mock import MagicMock, patch

import pytest
import requests

import shared.openalex_client as oa
from shared.cache import content_key, write_cache
from shared.openalex_client import (
    author_matches,
    extract_author_year_patterns,
    find_all_candidates,
)


# ── extract_author_year_patterns ──────────────────────────────────────────────

class TestExtractAuthorYearPatterns:
    @pytest.mark.parametrize("text,surname,year", [
        ("Smith (2020) found that ego depletion exists.", "smith", 2020),
        ("Baumeister et al. (1998) showed self-control.", "baumeister", 1998),
    ])
    def test_citation_forms_are_extracted(self, text, surname, year):
        patterns = extract_author_year_patterns(text)
        assert any(p["surname"] == surname and p["year"] == year for p in patterns)

    def test_multi_author_pattern(self):
        patterns = extract_author_year_patterns("Jones and Smith (2015) replicated the effect.")
        assert len(patterns) >= 1

    @pytest.mark.parametrize("text,kwargs,rejected", [
        ("Smith (2025) tested this", {"max_year": 2022}, lambda p: p["year"] == 2025),
        ("Darwin 1859 described evolution", {}, lambda p: p["year"] < 1900),
    ])
    def test_out_of_window_years_are_filtered(self, text, kwargs, rejected):
        patterns = extract_author_year_patterns(text, **kwargs)
        assert not any(rejected(p) for p in patterns)

    @pytest.mark.parametrize("text", [
        "The trial ran from May and June 2018 with no other citations.",
        "The report was filed in May, 2018.",
        "The event occurred on Friday, 2018.",
    ])
    def test_month_and_weekday_names_are_not_authors(self, text):
        assert extract_author_year_patterns(text) == []

    def test_real_surname_that_is_not_a_stopword_still_matches(self):
        patterns = extract_author_year_patterns("Friday et al. (2018) is not a real name, "
                                                  "but Smith (2018) is.")
        assert any(p["surname"] == "smith" and p["year"] == 2018 for p in patterns)


class TestStrictBareGate:
    """strict_bare=True must drop single_bare false matches (months/structural
    words) while keeping genuine bare and parenthesised citations, and must not
    change default (Stage-3) behaviour."""

    @pytest.mark.parametrize("text", [
        "Data were collected between January 2020 and March 2020.",
        "The July 2012 wave of the survey was analysed.",
        "The Beijing Eye Study 2011 was a population-based survey.",
        "Cases were recorded between 1966 and 1976 in the cohort.",
        "The COVID 2019 pandemic disrupted data collection.",
        "Analyses were run in Spring 2020 for the course.",
    ])
    def test_strict_drops_false_bare_matches(self, text):
        assert extract_author_year_patterns(text, strict_bare=True) == []

    def test_month_bare_dropped_even_without_strict(self):
        # Month/weekday tokens are rejected UNCONDITIONALLY (see _NAME_STOPWORDS):
        # a month is never an author surname, so the match is wrong in Stage 3 too.
        # The strict_bare gate remains opt-in for the *other* blacklist categories
        # (structural words, disease acronyms, sentence-initial function words).
        patterns = extract_author_year_patterns("Collected in January 2020 overall.")
        assert patterns == []

    def test_default_still_matches_non_month_structural_bare(self):
        # Confirms strict_bare really is opt-in for the non-month categories:
        # "Study 2019" still matches by default, and only drops under strict_bare.
        text = "Reported in Study 2019 overall."
        assert any(p["year"] == 2019 for p in extract_author_year_patterns(text))
        assert extract_author_year_patterns(text, strict_bare=True) == []

    def test_strict_keeps_real_bare_citation(self):
        patterns = extract_author_year_patterns(
            "This diverges from Smith 2019, who found the opposite.",
            strict_bare=True,
        )
        assert any(p["surname"] == "smith" and p["year"] == 2019 for p in patterns)

    def test_strict_keeps_comma_bare_citation(self):
        patterns = extract_author_year_patterns(
            "As reported by Brown, 2018, the effect held.",
            strict_bare=True,
        )
        assert any(p["surname"] == "brown" and p["year"] == 2018 for p in patterns)

    def test_strict_keeps_paren_citation(self):
        # single_paren is unaffected by strict_bare.
        patterns = extract_author_year_patterns(
            "We replicated Smith (2010).", strict_bare=True,
        )
        assert any(p["surname"] == "smith" and p["year"] == 2010 for p in patterns)

    def test_strict_keeps_etal_and_multi(self):
        patterns = extract_author_year_patterns(
            "Following Baumeister et al. 2007 and Jones and Lee 2015.",
            strict_bare=True,
        )
        years = {p["year"] for p in patterns}
        assert 2007 in years and 2015 in years

    def test_strict_bare_max_year_still_applies(self):
        patterns = extract_author_year_patterns(
            "See Smith 2030 for details.", max_year=2022, strict_bare=True,
        )
        assert not any(p["year"] == 2030 for p in patterns)


# ── author_matches ────────────────────────────────────────────────────────────

class TestAuthorMatches:
    @pytest.mark.parametrize("cited,refs", [
        ("smith", ["Smith"]),                 # exact
        ("SMITH", ["smith"]),                 # case-insensitive
        ("baum", ["Baumeister"]),             # cited is a prefix of the ref (≥ min_prefix=3)
        ("johnson", ["John"]),                # ref is a prefix of the cited surname
        ("smitt", ["Smith"]),                 # near-prefix: differ only at the last char
    ])
    def test_matching_rules(self, cited, refs):
        assert author_matches(cited, refs) is True

    @pytest.mark.parametrize("cited,refs", [
        ("jones", ["Smith", "Baumeister"]),   # unrelated surname
        ("", ["Smith"]),                      # no cited surname at all
        ("sm", ["Smith"]),                    # shorter than min_prefix=3 → no prefix match
    ])
    def test_non_matches(self, cited, refs):
        assert author_matches(cited, refs) is False


# ── find_all_candidates ───────────────────────────────────────────────────────

_REFS = [
    {
        "id": "https://openalex.org/W111",
        "doi": "https://doi.org/10.1000/smith2010",
        "title": "Smith Study on Cognitive Bias",
        "publication_year": 2010,
        "authorships": [{"author": {"display_name": "John Smith"}}],
    },
    {
        "id": "https://openalex.org/W222",
        "doi": "https://doi.org/10.1000/jones2015",
        "title": "Jones Study on Memory",
        "publication_year": 2015,
        "authorships": [{"author": {"display_name": "Alice Jones"}}],
    },
]


class TestFindAllCandidates:
    def _run(self, tmp_path, abstract, doi_r="10.9999/rep", year_r=2020, oa_id="W999"):
        with patch("shared.openalex_client.OA_CACHE_DIR", tmp_path), \
             patch("shared.openalex_client.fetch_referenced_works_metadata",
                   return_value=_REFS):
            return find_all_candidates(doi_r, oa_id, "", abstract, year_r, "")

    def test_correct_candidate_found(self, tmp_path):
        cands = self._run(tmp_path, "We replicated Smith (2010) and found consistent results.")
        dois = [c["doi"] for c in cands]
        assert "10.1000/smith2010" in dois

    @pytest.mark.parametrize("cited_year", [2009, 2011])
    def test_year_tolerance_one_either_way(self, tmp_path, cited_year):
        """A pattern citing 2009 or 2011 should match a 2010 reference (±1 window)."""
        refs = [{
            "id": "https://openalex.org/W333",
            "doi": "https://doi.org/10.1000/smith2010",
            "title": "Smith Study",
            "publication_year": 2010,
            "authorships": [{"author": {"display_name": "John Smith"}}],
        }]
        with patch("shared.openalex_client.OA_CACHE_DIR", tmp_path), \
             patch("shared.openalex_client.fetch_referenced_works_metadata", return_value=refs):
            cands = find_all_candidates(
                "10.9999/rep", "W999", "",
                f"We replicated Smith ({cited_year}) in our study.", 2020, "",
            )
        assert "10.1000/smith2010" in [c["doi"] for c in cands]

    def test_no_citation_pattern_returns_empty(self, tmp_path):
        """When the abstract has no author-year patterns, no candidates are returned."""
        with patch("shared.openalex_client.OA_CACHE_DIR", tmp_path), \
             patch("shared.openalex_client.fetch_referenced_works_metadata",
                   return_value=_REFS):
            cands = find_all_candidates(
                "10.9999/rep", "W999", "",
                "We conducted a study with no cited prior work.", 2020, "",
            )
        assert cands == []

    def test_self_doi_excluded(self, tmp_path):
        """The replication paper's own DOI must not appear in the candidate list."""
        refs_with_self = _REFS + [{
            "id": "https://openalex.org/W444",
            "doi": "https://doi.org/10.9999/rep",
            "title": "This Paper Itself",
            "publication_year": 2010,
            "authorships": [{"author": {"display_name": "John Smith"}}],
        }]
        with patch("shared.openalex_client.OA_CACHE_DIR", tmp_path), \
             patch("shared.openalex_client.fetch_referenced_works_metadata",
                   return_value=refs_with_self):
            cands = find_all_candidates(
                "10.9999/rep", "W999", "",
                "We replicated Smith (2010).", 2020, "",
            )
        assert "10.9999/rep" not in [c["doi"] for c in cands]

    def test_results_cached_second_call_skips_api(self, tmp_path):
        """Second call with the same doi_r must return from cache without calling the API."""
        with patch("shared.openalex_client.OA_CACHE_DIR", tmp_path), \
             patch("shared.openalex_client.fetch_referenced_works_metadata",
                   return_value=_REFS) as mock_fetch:
            find_all_candidates("10.9999/rep", "W999", "", "Smith (2010) studied this.", 2020, "")
            find_all_candidates("10.9999/rep", "W999", "", "Smith (2010) studied this.", 2020, "")
        assert mock_fetch.call_count == 1

    def test_different_abstract_misses_the_cache(self, tmp_path):
        """The three call sites pass different titles/abstracts for the same DOI, and
        the extracted patterns — hence the candidate list — follow from them."""
        with patch("shared.openalex_client.OA_CACHE_DIR", tmp_path), \
             patch("shared.openalex_client.fetch_referenced_works_metadata",
                   return_value=_REFS) as mock_fetch:
            find_all_candidates("10.9999/rep", "W999", "", "Smith (2010) studied this.", 2020, "")
            find_all_candidates("10.9999/rep", "W999", "", "Jones (2015) studied this.", 2020, "")
        assert mock_fetch.call_count == 2

    def test_different_title_misses_the_cache(self, tmp_path):
        with patch("shared.openalex_client.OA_CACHE_DIR", tmp_path), \
             patch("shared.openalex_client.fetch_referenced_works_metadata",
                   return_value=_REFS) as mock_fetch:
            find_all_candidates("10.9999/rep", "W999", "A replication of Smith (2010)",
                                "no patterns here", 2020, "")
            find_all_candidates("10.9999/rep", "W999", "A replication of Jones (2015)",
                                "no patterns here", 2020, "")
        assert mock_fetch.call_count == 2


# ── Title-search caching (audit E4) ──────────────────────────────────────────
# Both title searches fire several times for one row and a miss is the common
# answer, so the miss has to be cached too or most of the traffic stays uncached.

class TestTitleSearchCaching:
    def test_crossref_hit_is_cached(self, tmp_path):
        hit = {"doi": "10.9/orig", "title": "Time flies", "year": 2010}
        with patch("shared.openalex_client.OA_CACHE_DIR", tmp_path), \
             patch("shared.openalex_client._search_crossref_by_title_live",
                   return_value=hit) as live:
            first  = oa._search_crossref_by_title("Time flies", "2010")
            second = oa._search_crossref_by_title("Time flies", "2010")
        assert first == second == hit
        assert live.call_count == 1

    def test_crossref_miss_is_cached(self, tmp_path):
        with patch("shared.openalex_client.OA_CACHE_DIR", tmp_path), \
             patch("shared.openalex_client._search_crossref_by_title_live",
                   return_value=None) as live:
            assert oa._search_crossref_by_title("Nothing here", "") is None
            assert oa._search_crossref_by_title("Nothing here", "") is None
        assert live.call_count == 1

    def test_openalex_search_is_cached_per_title_and_year(self, tmp_path):
        with patch("shared.openalex_client.OA_CACHE_DIR", tmp_path), \
             patch("shared.openalex_client._search_openalex_by_title_live",
                   return_value=None) as live:
            oa._search_openalex_by_title("A title", "2010")
            oa._search_openalex_by_title("A title", "2011")
            oa._search_openalex_by_title("A title", "2010")
        assert live.call_count == 2

    def test_crossref_transport_failure_is_not_cached(self, tmp_path):
        """A provider that never answered has not said "no match" — caching its
        silence would pin the outage for every future run."""
        with patch("shared.openalex_client.OA_CACHE_DIR", tmp_path), \
             patch("shared.openalex_client._search_crossref_by_title_live",
                   side_effect=oa._TitleSearchUnavailable("timeout")) as live:
            assert oa._search_crossref_by_title("Time flies", "2010") is None
            assert oa._search_crossref_by_title("Time flies", "2010") is None
        assert live.call_count == 2
        assert list(tmp_path.glob("titlesearch_*")) == []

    def test_crossref_request_exception_raises_unavailable(self):
        with patch("shared.openalex_client.requests.get",
                   side_effect=RuntimeError("connection reset")):
            with pytest.raises(oa._TitleSearchUnavailable):
                oa._search_crossref_by_title_live("Time flies", "2010")

    def test_openalex_hit_carries_the_bare_work_id(self):
        """An original OpenAlex indexes without a DOI has no other identity, so the
        title search must hand back its W-id rather than only the (empty) DOI."""
        work = {"id": "https://openalex.org/W2884670852", "doi": None,
                "title": "Gender Advertisements", "publication_year": 1979,
                "authorships": [{"author": {"display_name": "Erving Goffman"}}],
                "primary_location": {}, "biblio": {}}
        with patch("shared.openalex_client._oa_get", return_value={"results": [work]}):
            hit = oa._search_openalex_by_title_live("Gender Advertisements", "1979")
        assert hit["openalex_id"] == "W2884670852"
        assert hit["doi"] == ""

    def test_crossref_hit_carries_an_empty_work_id_and_the_same_shape(self):
        item = {"DOI": "10.9/orig", "title": ["Time flies"],
                "issued": {"date-parts": [[2010]]}}
        with patch("shared.openalex_client.requests.get") as get:
            get.return_value.status_code = 200
            get.return_value.json.return_value = {"message": {"items": [item]}}
            crossref_hit = oa._search_crossref_by_title_live("Time flies", "2010")

        work = {"id": "https://openalex.org/W1", "doi": "https://doi.org/10.9/orig",
                "title": "Time flies", "publication_year": 2010,
                "authorships": [], "primary_location": {}, "biblio": {}}
        with patch("shared.openalex_client._oa_get", return_value={"results": [work]}):
            openalex_hit = oa._search_openalex_by_title_live("Time flies", "2010")

        assert crossref_hit["openalex_id"] == "", "CrossRef has no OpenAlex ids"
        assert set(crossref_hit) == set(openalex_hit)

    def test_old_cache_entries_are_not_reused(self, tmp_path):
        """Entries written before openalex_id was returned must miss, not silently
        strip the work id off every original already searched."""
        with patch("shared.openalex_client.OA_CACHE_DIR", tmp_path):
            stale = content_key("titlesearch_openalex", "", "A title", "2010")
            write_cache(tmp_path, stale, {"hit": {"doi": "10.9/orig", "title": "A title"}})
            with patch("shared.openalex_client._search_openalex_by_title_live",
                       return_value={"doi": "", "openalex_id": "W1"}) as live:
                hit = oa._search_openalex_by_title("A title", "2010")
        assert hit["openalex_id"] == "W1"
        assert live.call_count == 1

    def test_openalex_no_response_raises_but_empty_results_is_a_miss(self):
        with patch("shared.openalex_client._oa_get", return_value=None):
            with pytest.raises(oa._TitleSearchUnavailable):
                oa._search_openalex_by_title_live("Time flies", "2010")
        with patch("shared.openalex_client._oa_get", return_value={"results": []}):
            assert oa._search_openalex_by_title_live("Time flies", "2010") is None


class TestWorkLookupIsSharedBetweenFetchers:
    """fetch_openalex_by_doi and fetch_openalex_full_metadata used to request the
    same work under two different cache keys, doubling round-trips per doi_o."""

    _WORK = {
        "id": "https://openalex.org/W1", "doi": "https://doi.org/10.9/orig",
        "title": "Original", "publication_year": 2010,
        "authorships": [{"author": {"display_name": "Jane Smith"}}],
        "primary_location": {"source": {"display_name": "Journal"}},
        "biblio": {"volume": "1", "issue": "2",
                   "first_page": "3", "last_page": "4"},
    }

    def test_one_request_serves_both_shapes(self, tmp_path):
        with patch("shared.openalex_client.OA_CACHE_DIR", tmp_path), \
             patch("shared.openalex_client._oa_get",
                   return_value={"results": [self._WORK]}) as get:
            cand = oa.fetch_openalex_by_doi("10.9/orig")
            meta = oa.fetch_openalex_full_metadata("10.9/orig")
        assert get.call_count == 1
        assert cand["doi"] == "10.9/orig"
        assert meta["journal"] == "Journal"

    def test_a_failed_request_is_not_cached(self, tmp_path):
        with patch("shared.openalex_client.OA_CACHE_DIR", tmp_path), \
             patch("shared.openalex_client._oa_get", return_value=None) as get:
            assert oa.fetch_openalex_by_doi("10.9/orig") is None
            assert oa.fetch_openalex_by_doi("10.9/orig") is None
        assert get.call_count == 2


class TestOpenCitationsNoAnswerIsNotAnEmptyList:
    """A COCI outage, or a failed OpenAlex batch part-way through resolving the
    cited DOIs, must come back as None rather than as a paper with no
    references — and must never be cached as one."""

    @staticmethod
    def _coci(cited: list[str]):
        resp = MagicMock()
        resp.json.return_value = [{"cited": d} for d in cited]
        return resp

    @pytest.mark.parametrize("coci_fails", [True, False])
    def test_no_answer_returns_none_and_is_not_cached(self, tmp_path, coci_fails):
        get_kwargs = ({"side_effect": requests.exceptions.Timeout("boom")}
                      if coci_fails else {"return_value": self._coci(["10.1/a"])})
        with patch("shared.openalex_client.OA_CACHE_DIR", tmp_path), \
             patch("shared.openalex_client.throttle"), \
             patch("shared.openalex_client.requests.get", **get_kwargs), \
             patch("shared.openalex_client._oa_get", return_value=None):
            assert oa.fetch_opencitations_references("10.9/rep") is None
        assert list(tmp_path.glob("ocrefs_*.json")) == []

    def test_no_open_references_is_an_answer_and_is_cached(self, tmp_path):
        with patch("shared.openalex_client.OA_CACHE_DIR", tmp_path), \
             patch("shared.openalex_client.throttle"), \
             patch("shared.openalex_client.requests.get",
                   return_value=self._coci([])) as get:
            assert oa.fetch_opencitations_references("10.9/rep") == []
            assert oa.fetch_opencitations_references("10.9/rep") == []
        assert get.call_count == 1


class TestReferenceFetchNoAnswerIsNotAnEmptyList:
    """The same seam on the primary path: a work lookup or a metadata batch that
    never answered is None, not a work that cites nothing, and is not cached.
    find_all_candidates passes that distinction on rather than caching an empty
    candidate list under a content key that will never change."""

    _WORK = {"referenced_works": [f"https://openalex.org/W{i}" for i in range(60)]}

    @pytest.mark.parametrize("responses", [
        [None],                       # the work lookup never answered
        [_WORK, {"results": []}, None],   # the second metadata batch never answered
    ])
    def test_no_answer_returns_none_and_is_not_cached(self, tmp_path, responses):
        with patch("shared.openalex_client.OA_CACHE_DIR", tmp_path), \
             patch("shared.openalex_client._oa_get", side_effect=responses):
            assert oa.fetch_referenced_works_metadata("W999") is None
        assert list(tmp_path.glob("refs_*.json")) == []

    def test_a_work_that_cites_nothing_is_an_answer_and_is_cached(self, tmp_path):
        with patch("shared.openalex_client.OA_CACHE_DIR", tmp_path), \
             patch("shared.openalex_client._oa_get",
                   return_value={"referenced_works": []}) as get:
            assert oa.fetch_referenced_works_metadata("W999") == []
            assert oa.fetch_referenced_works_metadata("W999") == []
        assert get.call_count == 1

    def test_find_all_candidates_passes_the_no_answer_on(self, tmp_path):
        with patch("shared.openalex_client.OA_CACHE_DIR", tmp_path), \
             patch("shared.openalex_client.fetch_referenced_works_metadata",
                   return_value=None):
            assert find_all_candidates("10.9/rep", "W999", "",
                                       "We replicated Smith (2010).", 2020, "") is None
        assert list(tmp_path.glob("*.json")) == []


class TestAFilterValueOpenAlexWillAccept:
    """Every character in this class produced an HTTP 400 that `_oa_get` reported as
    "request failed after retries" — which reads downstream as an outage, and before
    the retry rule as "no such paper"."""

    def test_a_question_mark_is_stripped_because_it_is_a_wildcard(self):
        """A stemmed field rejects `?` outright, and titles that end in a question are
        ordinary. Four of five 400s in a 100-work run on 2026-08-07 were this."""
        assert oa._openalex_filter_value(
            "Are STEM Faculty Biased Against Female Applicants?"
        ) == "Are STEM Faculty Biased Against Female Applicants"
        assert "*" not in oa._openalex_filter_value("Smith* et al")

    def test_a_comma_is_stripped_because_it_separates_filters(self):
        assert oa._openalex_filter_value("Toya and Skidmore, 2007, Economic") == \
            "Toya and Skidmore 2007 Economic"

    def test_punctuation_openalex_accepts_is_left_alone(self):
        """Probed against the live API on 2026-08-07: parentheses, colons,
        apostrophes, percent signs and ampersands all answer 200."""
        assert oa._openalex_filter_value("(when) do 50% of Smith's & Jones: tests") == \
            "(when) do 50% of Smith's & Jones: tests"


class TestTheAuthorAndYearShortlist:
    _HIT = {"meta": {"count": 3},
            "results": [{"id": "https://openalex.org/W1", "doi": "https://doi.org/10.1/a",
                         "title": "The right paper", "publication_year": 2010,
                         "authorships": [], "cited_by_count": 300}]}

    def test_a_failed_narrowing_keeps_the_broad_answer(self, tmp_path):
        """The broad query answered; only the attempt to shorten its list failed.
        Reporting that as unavailable wrote api_error over four works whose shortlist
        was in hand."""
        broad = {"meta": {"count": 50}, "results": self._HIT["results"]}
        with patch("shared.openalex_client.OA_CACHE_DIR", tmp_path), \
             patch("shared.openalex_client._author_year_query",
                   side_effect=[broad, None]):
            candidates, total, unavailable = oa.author_year_candidates(
                "smith", 2010, topic="a topic")
        assert unavailable is False
        assert total == 50 and [c["doi"] for c in candidates] == ["10.1/a"]

    def test_a_silent_openalex_is_not_an_empty_shortlist(self, tmp_path):
        with patch("shared.openalex_client.OA_CACHE_DIR", tmp_path), \
             patch("shared.openalex_client._author_year_query", return_value=None):
            assert oa.author_year_candidates("smith", 2010) == ([], 0, True)
        assert list(tmp_path.glob("authoryear_*.json")) == []

    def test_the_shortlist_is_cached_and_the_query_runs_once(self, tmp_path):
        with patch("shared.openalex_client.OA_CACHE_DIR", tmp_path), \
             patch("shared.openalex_client._author_year_query",
                   return_value=self._HIT) as query:
            first = oa.author_year_candidates("smith", 2010)
            second = oa.author_year_candidates("smith", 2010)
        assert first == second and query.call_count == 1


class TestNarrowingAddsRatherThanReplaces:
    """Which words of a title carry its topic is a guess, and a wrong guess returns a
    short list that does not contain the paper. Adding cannot hide an answer;
    replacing can."""

    @staticmethod
    def _work(i, title):
        return {"id": f"https://openalex.org/W{i}", "doi": f"https://doi.org/10.1/{i}",
                "title": title, "publication_year": 2012, "authorships": [],
                "cited_by_count": 100 - i}

    def test_the_narrowed_hits_lead_and_the_broad_ones_survive(self, tmp_path):
        broad = {"meta": {"count": 400},
                 "results": [self._work(i, f"broad {i}") for i in range(3)]}
        narrow = {"meta": {"count": 2},
                  "results": [self._work(9, "the topical one")]}
        with patch("shared.openalex_client.OA_CACHE_DIR", tmp_path), \
             patch("shared.openalex_client._author_year_query",
                   side_effect=[broad, narrow]):
            candidates, total, _ = oa.author_year_candidates(
                ["anderson"], 2012, topic="status overconfidence in groups")
        assert [c["title"] for c in candidates] == [
            "the topical one", "broad 0", "broad 1", "broad 2"]
        # The count reported is the BROAD one: it is what "none of these" was said
        # against, and the narrowing does not shrink the population.
        assert total == 400

    def test_a_narrowing_that_finds_nothing_leaves_the_broad_list_alone(self, tmp_path):
        broad = {"meta": {"count": 400},
                 "results": [self._work(i, f"broad {i}") for i in range(3)]}
        empty = {"meta": {"count": 0}, "results": []}
        with patch("shared.openalex_client.OA_CACHE_DIR", tmp_path), \
             patch("shared.openalex_client._author_year_query",
                   side_effect=[broad, empty, empty]):
            candidates, _, _ = oa.author_year_candidates(
                ["anderson"], 2012, topic="status overconfidence in groups")
        assert [c["title"] for c in candidates] == ["broad 0", "broad 1", "broad 2"]


class TestTopicWordsAndAccents:
    def test_the_cited_authors_own_name_is_not_a_topic_word(self):
        """"Conceptual Replication (Young et al., 2016, Study 1)" has no other word of
        four letters, and an author's name in a title field finds the papers that
        discuss them, not the papers they wrote."""
        assert "young" not in oa._topic_words(
            "Conceptual Replication (Young et al., 2016, Study 1) of moral judgment",
            3, ["young"])

    def test_replication_vocabulary_carries_no_topic(self):
        assert oa._topic_words(
            "A Preregistered Replication and Extension of the anchoring effect", 2,
            []) == "anchoring"

    def test_an_accent_is_folded_for_the_author_filter(self):
        assert oa._fold_accents("Zárate") == "Zarate"
        assert oa._fold_accents("Häubl") == "Haubl"


class TestCrossRefLeadsAndOpenAlexFillsIn:
    """CrossRef is free and OpenAlex bills at 10x a filter query, so CrossRef is asked
    first — but it ENDS the search only when it has identified the paper, which is when
    one of its hits carries every surname the citation named. Treating a partial answer
    as a whole one lost nine links on the dev sample while gaining eight."""

    @staticmethod
    def _cr(*author_lists):
        return [{"doi": f"10.1/c{i}", "openalex_id": "", "title": f"cr {i}",
                 "year": 2015, "authors": list(a), "first_author": a[0].lower(),
                 "journal": "J", "cited_by": 5}
                for i, a in enumerate(author_lists)]

    def test_a_hit_with_every_cited_name_ends_the_search(self, tmp_path):
        with patch("shared.openalex_client.OA_CACHE_DIR", tmp_path), \
             patch("shared.openalex_client._crossref_author_year",
                   return_value=self._cr(["Jones", "Macken"])), \
             patch("shared.openalex_client._author_year_query") as query:
            candidates, _, _ = oa.author_year_candidates(
                ["jones", "macken"], 1995, topic="irrelevant speech spatial location")
        assert query.call_count == 0, "a free answer that identified the paper still " \
                                      "paid OpenAlex"
        assert [c["title"] for c in candidates] == ["cr 0"]

    def test_a_partial_answer_leads_a_pool_openalex_also_fills(self, tmp_path):
        broad = {"meta": {"count": 3},
                 "results": [{"id": "https://openalex.org/W9",
                              "doi": "https://doi.org/10.9/oa", "title": "the right one",
                              "publication_year": 2015, "authorships": [],
                              "cited_by_count": 99}]}
        with patch("shared.openalex_client.OA_CACHE_DIR", tmp_path), \
             patch("shared.openalex_client._crossref_author_year",
                   return_value=self._cr(["Turri"])), \
             patch("shared.openalex_client._author_year_query", return_value=broad):
            candidates, _, _ = oa.author_year_candidates(
                ["turri", "buckwalter", "blouw"], 2015, topic="knowledge attribution")
        assert [c["title"] for c in candidates] == ["cr 0", "the right one"]

    def test_a_silent_openalex_leaves_crossrefs_partial_answer_standing(self, tmp_path):
        with patch("shared.openalex_client.OA_CACHE_DIR", tmp_path), \
             patch("shared.openalex_client._crossref_author_year",
                   return_value=self._cr(["Turri"])), \
             patch("shared.openalex_client._author_year_query", return_value=None):
            candidates, _, unavailable = oa.author_year_candidates(
                ["turri", "buckwalter"], 2015, topic="knowledge attribution")
        assert unavailable is False and [c["title"] for c in candidates] == ["cr 0"]
