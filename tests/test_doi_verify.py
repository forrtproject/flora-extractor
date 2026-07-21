"""Unit tests for shared/doi_verify.py — all HTTP mocked, no live calls."""
from unittest.mock import MagicMock, patch

import pytest
import requests


def _resp(status=200, payload=None):
    m = MagicMock()
    m.status_code = status
    m.json.return_value = payload if payload is not None else {}
    if status >= 400:
        m.raise_for_status.side_effect = requests.HTTPError(f"HTTP {status}")
    else:
        m.raise_for_status.return_value = None
    return m


CROSSREF_WORK = {
    "message": {
        "title": ["Emotion word processing in the brain"],
        "author": [{"family": "Schindler", "given": "Sebastian"}],
        "published-print": {"date-parts": [[2019]]},
    }
}


@pytest.fixture(autouse=True)
def _isolate_cache(tmp_path, monkeypatch):
    import shared.doi_verify as dv
    monkeypatch.setattr(dv, "DOI_VERIFY_CACHE_DIR", tmp_path)
    monkeypatch.setattr(dv.time, "sleep", lambda *_: None)


class TestFetchDoiMetadata:
    def test_crossref_hit(self):
        from shared.doi_verify import fetch_doi_metadata
        with patch("shared.doi_verify.requests.get", return_value=_resp(200, CROSSREF_WORK)) as g:
            meta = fetch_doi_metadata("10.1111/psyp.13449")
        assert meta["registered"] is True
        assert meta["title"] == "Emotion word processing in the brain"
        assert meta["first_author_surname"] == "Schindler"
        assert meta["year"] == 2019
        assert meta["source"] == "crossref"
        assert "crossref.org" in g.call_args_list[0].args[0]

    def test_unregistered_doi_404(self):
        from shared.doi_verify import fetch_doi_metadata
        def fake_get(url, **kw):
            if "crossref.org" in url:
                return _resp(404)
            if "doi.org" in url:
                return _resp(404)
            return _resp(200, {"results": []})
        with patch("shared.doi_verify.requests.get", side_effect=fake_get):
            meta = fetch_doi_metadata("10.9999/does.not.exist")
        assert meta["registered"] is False

    def test_content_negotiation_fallback(self):
        # DOI 404s on CrossRef and is absent from OpenAlex, but resolves via
        # doi.org content negotiation (publisher-direct registrar).
        from shared.doi_verify import fetch_doi_metadata
        csl = {
            "title": "Some Obscure Publisher Article",
            "author": [{"family": "Kowalski"}],
            "issued": {"date-parts": [[2015]]},
        }
        def fake_get(url, **kw):
            if "crossref.org" in url:
                return _resp(404)
            if "openalex.org" in url:
                return _resp(200, {"results": []})
            if "doi.org" in url:
                return _resp(200, csl)
            return _resp(404)
        with patch("shared.doi_verify.requests.get", side_effect=fake_get):
            meta = fetch_doi_metadata("10.9999/publisher.direct")
        assert meta["registered"] is True
        assert meta["title"] == "Some Obscure Publisher Article"
        assert meta["first_author_surname"] == "Kowalski"
        assert meta["year"] == 2015
        assert meta["source"] == "content_negotiation"

    def test_datacite_doi_404_on_crossref_found_in_openalex(self):
        # Zenodo/OSF DOIs are DataCite-registered: CrossRef 404s on them but
        # OpenAlex indexes them — they must not be reported as unregistered.
        from shared.doi_verify import fetch_doi_metadata
        oa = {"results": [{
            "title": "Reproduction of a neural network analysis",
            "publication_year": 2020,
            "authorships": [{"author": {"display_name": "Ayo Adewale"}}],
        }]}
        def fake_get(url, **kw):
            if "crossref.org" in url:
                return _resp(404)
            return _resp(200, oa)
        with patch("shared.doi_verify.requests.get", side_effect=fake_get):
            meta = fetch_doi_metadata("10.5281/zenodo.18973411")
        assert meta["registered"] is True
        assert meta["source"] == "openalex"
        assert meta["first_author_surname"] == "Adewale"

    def test_crossref_down_openalex_fallback(self):
        from shared.doi_verify import fetch_doi_metadata
        oa = {"results": [{
            "title": "Emotion word processing in the brain",
            "publication_year": 2019,
            "authorships": [{"author": {"display_name": "Sebastian Schindler"}}],
        }]}
        def fake_get(url, **kw):
            if "crossref.org" in url:
                return _resp(500)
            return _resp(200, oa)
        with patch("shared.doi_verify.requests.get", side_effect=fake_get):
            meta = fetch_doi_metadata("10.1111/psyp.13449")
        assert meta["registered"] is True
        assert meta["source"] == "openalex"
        assert meta["first_author_surname"] == "Schindler"

    def test_both_apis_down_returns_none(self):
        from shared.doi_verify import fetch_doi_metadata
        with patch("shared.doi_verify.requests.get", return_value=_resp(500)):
            meta = fetch_doi_metadata("10.1111/psyp.13449")
        assert meta is None

    def test_result_is_cached(self):
        from shared.doi_verify import fetch_doi_metadata
        with patch("shared.doi_verify.requests.get", return_value=_resp(200, CROSSREF_WORK)) as g:
            fetch_doi_metadata("10.1111/psyp.13449")
            fetch_doi_metadata("10.1111/psyp.13449")
        assert g.call_count == 1


class TestMetadataMatches:
    def test_match(self):
        from shared.doi_verify import metadata_matches
        meta = {"registered": True, "title": "Emotion word processing in the brain",
                "first_author_surname": "Schindler", "year": 2019}
        assert metadata_matches(meta, "Emotion word processing in the brain", "Schindler", 2019)

    def test_year_off_by_one_ok(self):
        from shared.doi_verify import metadata_matches
        meta = {"registered": True, "title": "Emotion word processing in the brain",
                "first_author_surname": "Schindler", "year": 2019}
        assert metadata_matches(meta, "Emotion word processing in the brain", "Schindler", 2020)

    def test_different_title_fails(self):
        from shared.doi_verify import metadata_matches
        meta = {"registered": True, "title": "Cardiac responses to startling stimuli",
                "first_author_surname": "Other", "year": 2015}
        assert not metadata_matches(meta, "Emotion word processing in the brain", "Schindler", 2019)

    def test_unregistered_fails(self):
        from shared.doi_verify import metadata_matches
        meta = {"registered": False, "title": "", "first_author_surname": "", "year": None}
        assert not metadata_matches(meta, "Emotion word processing in the brain", "Schindler", 2019)


CROSSREF_SEARCH = {
    "message": {"items": [{
        "DOI": "10.1111/psyp.13449",
        "title": ["Emotion word processing in the brain"],
        "author": [{"family": "Schindler", "given": "Sebastian"}],
        "published-print": {"date-parts": [[2019]]},
    }]}
}

OPENALEX_SEARCH_NO_DOI = {
    "results": [{
        "id": "https://openalex.org/W123456789",
        "doi": None,
        "title": "An obscure book chapter about conformity",
        "publication_year": 1956,
        "authorships": [{"author": {"display_name": "Solomon Asch"}}],
    }]
}


class TestResolveDoiByMetadata:
    def test_crossref_search_hit(self):
        from shared.doi_verify import resolve_doi_by_metadata
        with patch("shared.doi_verify.requests.get", return_value=_resp(200, CROSSREF_SEARCH)):
            hit = resolve_doi_by_metadata("Emotion word processing in the brain", "Schindler", 2019)
        assert hit["doi"] == "10.1111/psyp.13449"
        assert hit["source"] == "crossref"

    def test_low_similarity_rejected(self):
        from shared.doi_verify import resolve_doi_by_metadata
        def fake_get(url, **kw):
            if "crossref.org" in url:
                return _resp(200, CROSSREF_SEARCH)
            return _resp(200, {"results": []})
        with patch("shared.doi_verify.requests.get", side_effect=fake_get):
            hit = resolve_doi_by_metadata("Completely unrelated paper on fish migration", "Garcia", 2003)
        assert hit is None

    def test_openalex_fallback_doiless_work(self):
        from shared.doi_verify import resolve_doi_by_metadata
        def fake_get(url, **kw):
            if "crossref.org" in url:
                return _resp(200, {"message": {"items": []}})
            return _resp(200, OPENALEX_SEARCH_NO_DOI)
        with patch("shared.doi_verify.requests.get", side_effect=fake_get):
            hit = resolve_doi_by_metadata("An obscure book chapter about conformity", "Asch", 1956)
        assert hit is not None
        assert hit["doi"] == ""
        assert hit["openalex_id"] == "https://openalex.org/W123456789"

    def test_no_title_returns_none(self):
        from shared.doi_verify import resolve_doi_by_metadata
        assert resolve_doi_by_metadata("", "Schindler", 2019) is None

    def test_client_error_4xx_not_retried(self):
        from shared.doi_verify import fetch_doi_metadata
        with patch("shared.doi_verify.requests.get", return_value=_resp(400)) as g:
            meta = fetch_doi_metadata("10.1111/psyp.13449")
        assert meta is None
        # one call to CrossRef + one to the OpenAlex fallback — no retries on 4xx
        assert g.call_count == 2

    def test_openalex_search_strips_question_mark(self):
        from shared.doi_verify import resolve_doi_by_metadata
        calls = []
        def fake_get(url, **kw):
            calls.append((url, kw.get("params", {})))
            if "crossref.org" in url:
                return _resp(200, {"message": {"items": []}})
            return _resp(200, {"results": []})
        with patch("shared.doi_verify.requests.get", side_effect=fake_get):
            resolve_doi_by_metadata("Does ego depletion exist? A replication attempt", "Hagger", 2016)
        oa_params = [p for u, p in calls if "openalex.org" in u]
        assert oa_params, "OpenAlex fallback was not called"
        assert "?" not in oa_params[0]["search"]

    def test_excludes_replication_own_doi(self):
        from shared.doi_verify import resolve_doi_by_metadata
        self_hit = {
            "message": {"items": [{
                "DOI": "10.5281/zenodo.18973410",
                "title": ["Reproduction of a neural network analysis"],
                "author": [{"family": "Adewale"}],
                "published-print": {"date-parts": [[2020]]},
            }]}
        }
        def fake_get(url, **kw):
            if "crossref.org" in url:
                return _resp(200, self_hit)
            return _resp(200, {"results": []})
        with patch("shared.doi_verify.requests.get", side_effect=fake_get):
            hit = resolve_doi_by_metadata("Reproduction of a neural network analysis",
                                          "Adewale", 2020,
                                          exclude_doi="10.5281/zenodo.18973410")
        assert hit is None

    def test_title_only_gap_accepts_dominant_paraphrase(self):
        # Real case 10.1111/psyp.13707: author_o and year_o were inherited from
        # the wrong DOI, title_o is paraphrased (jaccard 0.647 vs the true
        # original). The dominant-hit tier must still recover it.
        from shared.doi_verify import resolve_doi_by_metadata
        search = {"message": {"items": [
            {"DOI": "10.1111/psyp.13707",   # the replication itself (excluded)
             "title": ["Blunted cardiovascular reactivity to acute psychological stress predicts low behavioral persistence: replication"],
             "author": [{"family": "Whittaker"}], "issued": {"date-parts": [[2021]]}},
            {"DOI": "10.1111/psyp.13449",
             "title": ["Blunted cardiovascular responses to acute psychological stress predict low behavioral but not self-reported perseverance"],
             "author": [{"family": "Chauntry"}], "issued": {"date-parts": [[2019]]}},
            {"DOI": "10.1037/rel0000604",
             "title": ["Negative religious coping is associated with blunted cardiovascular reactivity"],
             "author": [{"family": "Dempsey"}], "issued": {"date-parts": [[2022]]}},
        ]}}
        def fake_get(url, **kw):
            if "crossref.org" in url:
                return _resp(200, search)
            return _resp(200, {"results": []})
        title_o = "Blunted cardiac reactivity to acute psychological stress predicts low behavioral but not self-reported perseverance"
        with patch("shared.doi_verify.requests.get", side_effect=fake_get):
            hit = resolve_doi_by_metadata(title_o, "", None,
                                          exclude_doi="10.1111/psyp.13707",
                                          title_only_gap=True)
        assert hit is not None
        assert hit["doi"] == "10.1111/psyp.13449"

    def test_excludes_doi_prefix_variants(self):
        # 10.1037/apl0000891.supp is the replication's own supplementary
        # material — prefix variants of doi_r must be excluded too.
        from shared.doi_verify import resolve_doi_by_metadata
        search = {"message": {"items": [{
            "DOI": "10.1037/apl0000891.supp",
            "title": ["Daily microbreaks in a self-regulatory resources lens (supplementary)"],
            "author": [{"family": "Kim"}],
            "issued": {"date-parts": [[2022]]},
        }]}}
        def fake_get(url, **kw):
            if "crossref.org" in url:
                return _resp(200, search)
            return _resp(200, {"results": []})
        with patch("shared.doi_verify.requests.get", side_effect=fake_get):
            hit = resolve_doi_by_metadata(
                "Daily microbreaks in a self-regulatory resources lens", "Kim", 2022,
                exclude_doi="10.1037/apl0000891")
        assert hit is None

    def test_rejects_hit_closer_to_replication_title(self):
        # A preprint replication's published version echoes the original's
        # title and is not excluded by DOI — reject hits whose title matches
        # the replication's own title better than the claimed original's.
        from shared.doi_verify import resolve_doi_by_metadata
        search = {"message": {"items": [{
            "DOI": "10.1177/0956797620955209",
            "title": ["Sick body, vigilant mind: a direct replication and extension"],
            "author": [{"family": "Vega"}],
            "issued": {"date-parts": [[2020]]},
        }]}}
        def fake_get(url, **kw):
            if "crossref.org" in url:
                return _resp(200, search)
            return _resp(200, {"results": []})
        with patch("shared.doi_verify.requests.get", side_effect=fake_get):
            hit = resolve_doi_by_metadata(
                "Sick body, vigilant mind: the biological immune system activates the behavioral immune system",
                "", None,
                exclude_doi="10.31234/osf.io/m6ghr",
                exclude_title="Sick body, vigilant mind: a direct replication and extension",
                title_only_gap=True)
        assert hit is None

    def test_rejects_corrigenda_and_errata(self):
        # A corrigendum title embeds the article title and scores high, but a
        # correction notice can never be the original study.
        from shared.doi_verify import resolve_doi_by_metadata
        search = {"message": {"items": [{
            "DOI": "10.1177/1368430220933248",
            "title": ["Corrigendum to Collective existential threat mediates White population decline's effect on defensive reactions"],
            "author": [{"family": "Bai"}],
            "issued": {"date-parts": [[2020]]},
        }]}}
        def fake_get(url, **kw):
            if "crossref.org" in url:
                return _resp(200, search)
            return _resp(200, {"results": []})
        with patch("shared.doi_verify.requests.get", side_effect=fake_get):
            hit = resolve_doi_by_metadata(
                "Collective existential threat mediates White population decline's effect on defensive reactions",
                "", None, title_only_gap=True)
        assert hit is None

    def test_title_only_gap_dedupes_same_doi_across_sources(self):
        # CrossRef and OpenAlex both return the same work — the duplicate must
        # not defeat the dominance check by tying with itself.
        from shared.doi_verify import resolve_doi_by_metadata
        cr = {"message": {"items": [
            {"DOI": "10.1111/psyp.13449",
             "title": ["Blunted cardiovascular responses to acute psychological stress predict low behavioral but not self-reported perseverance"],
             "author": [{"family": "Chauntry"}], "issued": {"date-parts": [[2019]]}},
        ]}}
        oa = {"results": [{
            "id": "https://openalex.org/W999",
            "doi": "https://doi.org/10.1111/psyp.13449",
            "title": "Blunted cardiovascular responses to acute psychological stress predict low behavioral but not self-reported perseverance",
            "publication_year": 2019,
            "authorships": [{"author": {"display_name": "Pip Chauntry"}}],
        }]}
        def fake_get(url, **kw):
            return _resp(200, cr if "crossref.org" in url else oa)
        title_o = "Blunted cardiac reactivity to acute psychological stress predicts low behavioral but not self-reported perseverance"
        with patch("shared.doi_verify.requests.get", side_effect=fake_get):
            hit = resolve_doi_by_metadata(title_o, "", None, title_only_gap=True)
        assert hit is not None
        assert hit["doi"] == "10.1111/psyp.13449"

    def test_title_only_gap_rejects_ambiguous_hits(self):
        from shared.doi_verify import resolve_doi_by_metadata
        search = {"message": {"items": [
            {"DOI": "10.1000/a", "title": ["Stress reactivity predicts perseverance in adults"],
             "author": [{"family": "Smith"}], "issued": {"date-parts": [[2019]]}},
            {"DOI": "10.1000/b", "title": ["Stress reactivity predicts perseverance in students"],
             "author": [{"family": "Jones"}], "issued": {"date-parts": [[2018]]}},
        ]}}
        def fake_get(url, **kw):
            if "crossref.org" in url:
                return _resp(200, search)
            return _resp(200, {"results": []})
        with patch("shared.doi_verify.requests.get", side_effect=fake_get):
            hit = resolve_doi_by_metadata("Stress reactivity predicts perseverance", "", None,
                                          title_only_gap=True)
        assert hit is None  # two near-equal hits — no dominant winner

    def test_negative_result_cached(self):
        from shared.doi_verify import resolve_doi_by_metadata
        empty = {"message": {"items": []}}
        def fake_get(url, **kw):
            return _resp(200, empty if "crossref.org" in url else {"results": []})
        with patch("shared.doi_verify.requests.get", side_effect=fake_get) as g:
            resolve_doi_by_metadata("Some unfindable title here", "Nobody", 1999)
            n_first = g.call_count
            resolve_doi_by_metadata("Some unfindable title here", "Nobody", 1999)
        assert g.call_count == n_first  # second call fully served from cache


class TestVerifyAndCorrect:
    """Statuses: verified, corrected, mismatch, no_doi, not_found,
    no_metadata, api_error, skipped."""

    TITLE  = "Emotion word processing in the brain"
    AUTHOR = "Schindler"
    YEAR   = 2019

    def test_verified(self):
        from shared import doi_verify as dv
        meta = {"registered": True, "title": self.TITLE,
                "first_author_surname": "Schindler", "year": 2019, "source": "crossref"}
        with patch.object(dv, "fetch_doi_metadata", return_value=meta):
            out = dv.verify_and_correct("10.1111/psyp.13449", self.TITLE, self.AUTHOR, self.YEAR)
        assert out["doi_o_verification"] == "verified"
        assert out["doi_o"] == "10.1111/psyp.13449"

    def test_corrected_real_case(self):
        # doi_r 10.1111/psyp.13707: LLM got title/author right but emitted
        # 10.1016/j.biopsycho.2015.07.014 (a different, registered paper).
        from shared import doi_verify as dv
        wrong_meta = {"registered": True, "title": "Cardiac responses to startling stimuli",
                      "first_author_surname": "Other", "year": 2015, "source": "crossref"}
        replacement = {"found": True, "doi": "10.1111/psyp.13449", "title": self.TITLE,
                       "year": 2019, "openalex_id": "", "source": "crossref"}
        with patch.object(dv, "fetch_doi_metadata", return_value=wrong_meta), \
             patch.object(dv, "resolve_doi_by_metadata", return_value=replacement):
            out = dv.verify_and_correct("10.1016/j.biopsycho.2015.07.014",
                                        self.TITLE, self.AUTHOR, self.YEAR)
        assert out["doi_o_verification"] == "corrected"
        assert out["doi_o"] == "10.1111/psyp.13449"
        assert "10.1016/j.biopsycho.2015.07.014" in out["evidence_note"]

    def test_corrected_via_yearless_retry(self):
        # year_o was inherited from the wrong DOI (2015), the real original is
        # 2019 — the year-constrained search fails, the yearless retry succeeds.
        from shared import doi_verify as dv
        wrong_meta = {"registered": True, "title": "Cardiac responses to startling stimuli",
                      "first_author_surname": "Other", "year": 2015, "source": "crossref"}
        replacement = {"found": True, "doi": "10.1111/psyp.13449", "title": self.TITLE,
                       "year": 2019, "openalex_id": "", "source": "crossref"}
        with patch.object(dv, "fetch_doi_metadata", return_value=wrong_meta), \
             patch.object(dv, "resolve_doi_by_metadata",
                          side_effect=[None, replacement]) as res:
            out = dv.verify_and_correct("10.1016/j.biopsycho.2015.07.014",
                                        self.TITLE, self.AUTHOR, 2015)
        assert out["doi_o_verification"] == "corrected"
        assert out["doi_o"] == "10.1111/psyp.13449"
        assert res.call_count == 2
        assert res.call_args_list[1].args[2] is None  # second call without year

    def test_no_yearless_retry_without_author(self):
        from shared import doi_verify as dv
        wrong_meta = {"registered": True, "title": "Cardiac responses to startling stimuli",
                      "first_author_surname": "Other", "year": 2015, "source": "crossref"}
        with patch.object(dv, "fetch_doi_metadata", return_value=wrong_meta), \
             patch.object(dv, "resolve_doi_by_metadata", return_value=None) as res:
            out = dv.verify_and_correct("10.1016/j.biopsycho.2015.07.014",
                                        self.TITLE, "", 2015)
        assert out["doi_o_verification"] == "mismatch"
        # yearless retry requires a known author — goes straight to the
        # title-only dominance tier instead
        assert res.call_count == 2
        assert res.call_args_list[1].kwargs.get("title_only_gap") is True

    def test_search_refinds_same_doi_means_verified(self):
        # Only year_o was wrong (inherited bad year); the search re-finds the
        # same DOI — that's a verification, not a correction.
        from shared import doi_verify as dv
        meta = {"registered": True, "title": self.TITLE,
                "first_author_surname": "Schindler", "year": 2010, "source": "crossref"}
        same = {"found": True, "doi": "10.1111/psyp.13449", "title": self.TITLE,
                "year": 2010, "openalex_id": "", "source": "crossref"}
        with patch.object(dv, "fetch_doi_metadata", return_value=meta), \
             patch.object(dv, "resolve_doi_by_metadata", return_value=same):
            out = dv.verify_and_correct("10.1111/psyp.13449", self.TITLE, self.AUTHOR, 2019)
        assert out["doi_o_verification"] == "verified"
        assert out["doi_o"] == "10.1111/psyp.13449"
        assert "year" in out["evidence_note"].lower()

    def test_mismatch_no_replacement(self):
        from shared import doi_verify as dv
        wrong_meta = {"registered": True, "title": "Cardiac responses to startling stimuli",
                      "first_author_surname": "Other", "year": 2015, "source": "crossref"}
        with patch.object(dv, "fetch_doi_metadata", return_value=wrong_meta), \
             patch.object(dv, "resolve_doi_by_metadata", return_value=None):
            out = dv.verify_and_correct("10.1016/j.biopsycho.2015.07.014",
                                        self.TITLE, self.AUTHOR, self.YEAR)
        assert out["doi_o_verification"] == "mismatch"
        assert out["doi_o"] == "10.1016/j.biopsycho.2015.07.014"

    def test_no_doi_blank_input_doiless_original(self):
        from shared import doi_verify as dv
        repl = {"found": True, "doi": "", "title": "An obscure book chapter",
                "year": 1956, "openalex_id": "https://openalex.org/W123", "source": "openalex"}
        with patch.object(dv, "resolve_doi_by_metadata", return_value=repl):
            out = dv.verify_and_correct("", "An obscure book chapter", "Asch", 1956)
        assert out["doi_o_verification"] == "no_doi"
        assert out["doi_o"] == ""
        assert "W123" in out["evidence_note"]

    def test_corrected_fills_blank_doi(self):
        from shared import doi_verify as dv
        repl = {"found": True, "doi": "10.1111/psyp.13449", "title": self.TITLE,
                "year": 2019, "openalex_id": "", "source": "crossref"}
        with patch.object(dv, "resolve_doi_by_metadata", return_value=repl):
            out = dv.verify_and_correct("", self.TITLE, self.AUTHOR, self.YEAR)
        assert out["doi_o_verification"] == "corrected"
        assert out["doi_o"] == "10.1111/psyp.13449"

    def test_not_found(self):
        from shared import doi_verify as dv
        with patch.object(dv, "resolve_doi_by_metadata", return_value=None):
            out = dv.verify_and_correct("", "Some unfindable title", "Nobody", 1999)
        assert out["doi_o_verification"] == "not_found"
        assert out["doi_o"] == ""

    def test_no_metadata(self):
        from shared import doi_verify as dv
        unreg = {"registered": False, "title": "", "first_author_surname": "",
                 "year": None, "source": "crossref"}
        with patch.object(dv, "fetch_doi_metadata", return_value=unreg), \
             patch.object(dv, "resolve_doi_by_metadata", return_value=None):
            out = dv.verify_and_correct("10.9999/nope", self.TITLE, self.AUTHOR, self.YEAR)
        assert out["doi_o_verification"] == "no_metadata"
        assert out["doi_o"] == "10.9999/nope"

    def test_api_error(self):
        from shared import doi_verify as dv
        with patch.object(dv, "fetch_doi_metadata", return_value=None):
            out = dv.verify_and_correct("10.1111/psyp.13449", self.TITLE, self.AUTHOR, self.YEAR)
        assert out["doi_o_verification"] == "api_error"
        assert out["doi_o"] == "10.1111/psyp.13449"

    def test_skipped(self):
        from shared import doi_verify as dv
        out = dv.verify_and_correct("", "", "", "")
        assert out["doi_o_verification"] == "skipped"


class TestVerifyRowHook:
    def _row(self, **over):
        row = {"doi_r": "10.1111/psyp.13707", "doi_o": "10.1016/j.biopsycho.2015.07.014",
               "title_o": "Emotion word processing in the brain", "authors_o": "Schindler",
               "year_o": "2019", "link_method": "llm_fulltext",
               "link_evidence": "existing evidence", "link_confidence": "high",
               "pair_id": "x", "ref_o": "old ref"}
        row.update(over)
        return row

    def test_corrected_updates_doi_pair_id_and_evidence(self):
        from extract.run_extract import _verify_row
        from shared.schema import make_pair_id
        v = {"doi_o_verification": "corrected", "doi_o": "10.1111/psyp.13449",
             "evidence_note": "DOI corrected: ..."}
        with patch("extract.run_extract.verify_and_correct", return_value=v), \
             patch("extract.run_extract._build_ref_o",
                   return_value=("new ref", "New Author", "@article{new}")):
            row = _verify_row(self._row())
        assert row["doi_o"] == "10.1111/psyp.13449"
        assert row["doi_o_verification"] == "corrected"
        assert row["pair_id"] == make_pair_id("10.1111/psyp.13707", "10.1111/psyp.13449")
        assert row["ref_o"] == "new ref"
        assert row["bibtex_ref_o"] == "@article{new}"
        assert "existing evidence" in row["link_evidence"]
        assert "DOI corrected" in row["link_evidence"]

    def test_mismatch_downgrades_confidence(self):
        from extract.run_extract import _verify_row
        v = {"doi_o_verification": "mismatch",
             "doi_o": "10.1016/j.biopsycho.2015.07.014", "evidence_note": "DOI mismatch: ..."}
        with patch("extract.run_extract.verify_and_correct", return_value=v):
            row = _verify_row(self._row())
        assert row["link_confidence"] == "low"
        assert row["doi_o"] == "10.1016/j.biopsycho.2015.07.014"

    def test_passes_doi_r_as_exclusion(self):
        from extract.run_extract import _verify_row
        v = {"doi_o_verification": "verified",
             "doi_o": "10.1016/j.biopsycho.2015.07.014", "evidence_note": ""}
        with patch("extract.run_extract.verify_and_correct", return_value=v) as vc:
            _verify_row(self._row())
        assert vc.call_args.kwargs.get("exclude_doi") == "10.1111/psyp.13707"

    def test_target_pending_skipped_no_api_call(self):
        from extract.run_extract import _verify_row
        with patch("extract.run_extract.verify_and_correct") as vc:
            row = _verify_row(self._row(link_method="target_pending", doi_o=""))
        vc.assert_not_called()
        assert row["doi_o_verification"] == "skipped"

    def test_verified_leaves_row_untouched(self):
        from extract.run_extract import _verify_row
        v = {"doi_o_verification": "verified",
             "doi_o": "10.1016/j.biopsycho.2015.07.014", "evidence_note": ""}
        with patch("extract.run_extract.verify_and_correct", return_value=v):
            row = _verify_row(self._row())
        assert row["doi_o_verification"] == "verified"
        assert row["link_evidence"] == "existing evidence"
        assert row["pair_id"] == "x"


class TestAuditDois:
    def _csv(self, tmp_path):
        import pandas as pd
        from shared.schema import EXTRACTED_COLS
        rows = []
        base = {c: "" for c in EXTRACTED_COLS}
        rows.append({**base, "doi_r": "10.1111/psyp.13707",
                     "doi_o": "10.1016/j.biopsycho.2015.07.014",
                     "title_o": "Emotion word processing in the brain",
                     "authors_o": "Schindler", "year_o": "2019",
                     "link_method": "llm_fulltext", "link_confidence": "high"})
        rows.append({**base, "doi_r": "10.2222/pending", "link_method": "target_pending"})
        path = tmp_path / "extracted.csv"
        pd.DataFrame(rows)[EXTRACTED_COLS].to_csv(path, index=False, encoding="utf-8-sig")
        return path

    def test_dry_run_reports_but_does_not_write(self, tmp_path):
        import pandas as pd
        from extract.audit_dois import audit_file
        v = {"doi_o_verification": "corrected", "doi_o": "10.1111/psyp.13449",
             "evidence_note": "DOI corrected: ..."}
        path = self._csv(tmp_path)
        before = path.read_text(encoding="utf-8-sig")
        with patch("extract.audit_dois.verify_and_correct", return_value=v), \
             patch("extract.audit_dois._build_ref_o", return_value=("ref", "Author", "@article{x}")):
            summary = audit_file(path, apply=False, report_path=tmp_path / "report.csv")
        assert summary["corrected"] == 1
        assert summary["skipped"] == 1
        assert path.read_text(encoding="utf-8-sig") == before
        report = pd.read_csv(tmp_path / "report.csv", dtype=str, encoding="utf-8-sig")
        assert "10.1111/psyp.13449" in report["proposed_doi_o"].tolist()

    def test_apply_writes_corrections(self, tmp_path):
        import pandas as pd
        from extract.audit_dois import audit_file
        from shared.schema import make_pair_id
        v = {"doi_o_verification": "corrected", "doi_o": "10.1111/psyp.13449",
             "evidence_note": "DOI corrected: ..."}
        path = self._csv(tmp_path)
        with patch("extract.audit_dois.verify_and_correct", return_value=v), \
             patch("extract.audit_dois._build_ref_o", return_value=("ref", "Author", "@article{x}")):
            audit_file(path, apply=True, report_path=tmp_path / "report.csv")
        df = pd.read_csv(path, dtype=str, encoding="utf-8-sig").fillna("")
        row = df[df["doi_r"] == "10.1111/psyp.13707"].iloc[0]
        assert row["doi_o"] == "10.1111/psyp.13449"
        assert row["doi_o_verification"] == "corrected"
        assert row["pair_id"] == make_pair_id("10.1111/psyp.13707", "10.1111/psyp.13449")
        pend = df[df["doi_r"] == "10.2222/pending"].iloc[0]
        assert pend["doi_o_verification"] == "skipped"

    def test_doi_filter(self, tmp_path):
        from extract.audit_dois import audit_file
        path = self._csv(tmp_path)
        with patch("extract.audit_dois.verify_and_correct") as vc:
            vc.return_value = {"doi_o_verification": "verified",
                               "doi_o": "10.1016/j.biopsycho.2015.07.014", "evidence_note": ""}
            summary = audit_file(path, apply=False, report_path=tmp_path / "report.csv",
                                 only_doi="10.1111/psyp.13707")
        assert vc.call_count == 1
        assert summary["verified"] == 1
