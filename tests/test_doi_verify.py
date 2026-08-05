"""Unit tests for shared/doi_verify.py — all HTTP mocked, no live calls."""
from contextlib import contextmanager
from unittest.mock import ANY, MagicMock, patch

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


@contextmanager
def _patch_get(**kw):
    """One fake requests.get for BOTH modules doi_verify reaches the network through.

    doi_verify calls CrossRef itself; its OpenAlex calls go through
    openalex_client._oa_get (throttle + key rotation + quota detection), so a fake
    patched into only one module lets the other issue a live request. The SAME mock
    object goes into both, so call_args_list still sees every call in order.
    """
    m = MagicMock(**kw)
    with patch("shared.doi_verify.requests.get", m), \
         patch("shared.openalex_client.requests.get", m), \
         patch("shared.openalex_client.throttle", lambda *a, **k: None):
        yield m


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
        with _patch_get(return_value=_resp(200, CROSSREF_WORK)) as g:
            meta = fetch_doi_metadata("10.1111/psyp.13449")
        assert meta["registered"] is True
        assert meta["title"] == "Emotion word processing in the brain"
        assert meta["first_author_surname"] == "Schindler"
        assert meta["year"] == 2019
        assert meta["source"] == "crossref"
        assert meta["type"] == ""   # this work reports none
        assert "crossref.org" in g.call_args_list[0].args[0]

    def test_unregistered_doi_404(self):
        from shared.doi_verify import fetch_doi_metadata
        def fake_get(url, **kw):
            if "crossref.org" in url:
                return _resp(404)
            if "doi.org" in url:
                return _resp(404)
            return _resp(200, {"results": []})
        with _patch_get(side_effect=fake_get):
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
        with _patch_get(side_effect=fake_get):
            meta = fetch_doi_metadata("10.9999/publisher.direct")
        assert meta["registered"] is True
        assert meta["title"] == "Some Obscure Publisher Article"
        assert meta["first_author_surname"] == "Kowalski"
        assert meta["year"] == 2015
        assert meta["source"] == "content_negotiation"

    @pytest.mark.parametrize("crossref_status,doi", [
        # CrossRef is down — OpenAlex answers instead.
        (500, "10.1111/psyp.13449"),
        # Zenodo/OSF DOIs are DataCite-registered: CrossRef 404s on them but
        # OpenAlex indexes them — they must not be reported as unregistered.
        (404, "10.5281/zenodo.18973411"),
    ])
    def test_openalex_answers_when_crossref_does_not(self, crossref_status, doi):
        from shared.doi_verify import fetch_doi_metadata
        oa = {"results": [{
            "title": "Emotion word processing in the brain",
            "publication_year": 2019,
            "authorships": [{"author": {"display_name": "Sebastian Schindler"}}],
        }]}
        def fake_get(url, **kw):
            if "crossref.org" in url:
                return _resp(crossref_status)
            return _resp(200, oa)
        with _patch_get(side_effect=fake_get):
            meta = fetch_doi_metadata(doi)
        assert meta["registered"] is True
        assert meta["source"] == "openalex"
        assert meta["first_author_surname"] == "Schindler"

    def test_both_apis_down_returns_none(self):
        from shared.doi_verify import fetch_doi_metadata
        with _patch_get(return_value=_resp(500)):
            meta = fetch_doi_metadata("10.1111/psyp.13449")
        assert meta is None

    def test_the_work_type_is_returned_by_either_source(self):
        """sanity_check --deep routes on this field, so both sources have to fill it
        — and the OpenAlex query has to ask for it."""
        from shared.doi_verify import fetch_doi_metadata
        work = {"message": {**CROSSREF_WORK["message"], "type": "dataset"}}
        with _patch_get(return_value=_resp(200, work)):
            meta = fetch_doi_metadata("10.7910/dvn/abcdef")
        assert meta["type"] == "dataset"

        oa = {"results": [{
            "title": "Author response: Some eLife paper",
            "publication_year": 2021,
            "authorships": [{"author": {"display_name": "Ada Lovelace"}}],
            "type": "peer-review",
        }]}
        def fake_get(url, **kw):
            return _resp(404) if "crossref.org" in url else _resp(200, oa)
        with _patch_get(side_effect=fake_get) as g:
            meta = fetch_doi_metadata("10.7554/elife.12345.041")
        assert meta["type"] == "peer-review"   # a non-article object, quarantined later
        oa_call = [c for c in g.call_args_list if "openalex.org" in c.args[0]][0]
        assert "type" in oa_call.kwargs["params"]["select"].split(",")

    def test_result_is_cached(self):
        from shared.doi_verify import fetch_doi_metadata
        with _patch_get(return_value=_resp(200, CROSSREF_WORK)) as g:
            fetch_doi_metadata("10.1111/psyp.13449")
            fetch_doi_metadata("10.1111/psyp.13449")
        assert g.call_count == 1


TITLE = "Emotion word processing in the brain"


class TestMetadataMatches:
    @pytest.mark.parametrize("meta,claimed_year,expected", [
        # Exact metadata, and the same metadata a year out — YEAR_TOLERANCE is 1.
        ({"registered": True, "title": TITLE,
          "first_author_surname": "Schindler", "year": 2019}, 2019, True),
        ({"registered": True, "title": TITLE,
          "first_author_surname": "Schindler", "year": 2019}, 2020, True),
        # A DOI that describes a different paper entirely.
        ({"registered": True, "title": "Cardiac responses to startling stimuli",
          "first_author_surname": "Other", "year": 2015}, 2019, False),
        # An unregistered DOI has nothing to match against.
        ({"registered": False, "title": "", "first_author_surname": "", "year": None},
         2019, False),
    ])
    def test_the_match_thresholds(self, meta, claimed_year, expected):
        from shared.doi_verify import metadata_matches
        assert bool(metadata_matches(meta, TITLE, "Schindler", claimed_year)) is expected


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
        with _patch_get(return_value=_resp(200, CROSSREF_SEARCH)):
            hit = resolve_doi_by_metadata("Emotion word processing in the brain", "Schindler", 2019)
        assert hit["doi"] == "10.1111/psyp.13449"
        assert hit["source"] == "crossref"

    def test_low_similarity_rejected(self):
        from shared.doi_verify import resolve_doi_by_metadata
        def fake_get(url, **kw):
            if "crossref.org" in url:
                return _resp(200, CROSSREF_SEARCH)
            return _resp(200, {"results": []})
        with _patch_get(side_effect=fake_get):
            hit = resolve_doi_by_metadata("Completely unrelated paper on fish migration", "Garcia", 2003)
        assert hit is None

    def test_openalex_fallback_doiless_work(self):
        from shared.doi_verify import resolve_doi_by_metadata
        def fake_get(url, **kw):
            if "crossref.org" in url:
                return _resp(200, {"message": {"items": []}})
            return _resp(200, OPENALEX_SEARCH_NO_DOI)
        with _patch_get(side_effect=fake_get):
            hit = resolve_doi_by_metadata("An obscure book chapter about conformity", "Asch", 1956)
        assert hit is not None
        assert hit["doi"] == ""
        assert hit["openalex_id"] == "https://openalex.org/W123456789"

    def test_client_error_4xx_not_retried(self):
        from shared.doi_verify import fetch_doi_metadata
        with _patch_get(return_value=_resp(400)) as g:
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
        with _patch_get(side_effect=fake_get):
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
        with _patch_get(side_effect=fake_get):
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
        with _patch_get(side_effect=fake_get):
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
        with _patch_get(side_effect=fake_get):
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
        with _patch_get(side_effect=fake_get):
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
        with _patch_get(side_effect=fake_get):
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
        with _patch_get(side_effect=fake_get):
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
        with _patch_get(side_effect=fake_get):
            hit = resolve_doi_by_metadata("Stress reactivity predicts perseverance", "", None,
                                          title_only_gap=True)
        assert hit is None  # two near-equal hits — no dominant winner

    def test_negative_result_cached(self):
        from shared.doi_verify import resolve_doi_by_metadata
        empty = {"message": {"items": []}}
        def fake_get(url, **kw):
            return _resp(200, empty if "crossref.org" in url else {"results": []})
        with _patch_get(side_effect=fake_get) as g:
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

    @pytest.mark.parametrize("title,author,year,expected", [
        # A title to search for, but nothing found.
        ("Some unfindable title", "Nobody", 1999, "not_found"),
        # No DOI and no title: there is no question to ask.
        ("", "", "", "skipped"),
    ])
    def test_a_blank_doi_without_a_hit(self, title, author, year, expected):
        from shared import doi_verify as dv
        with patch.object(dv, "resolve_doi_by_metadata", return_value=None):
            out = dv.verify_and_correct("", title, author, year)
        assert out["doi_o_verification"] == expected
        assert out["doi_o"] == ""


@pytest.mark.parametrize("new_status,prior_status,work_id,expected", [
    # The row's identity is its OpenAlex work id: a fresh "not_found" (all
    # verify_and_correct can ever say about a DOI-less original) must not
    # overwrite the recorded "no_doi" and turn the row into an audit blocker.
    ("not_found", "no_doi", "https://openalex.org/W123", True),
    ("not_found", "no_doi", "W123", True),
    # No work id — there is nothing to keep the row on, so let it through.
    ("not_found", "no_doi", "", False),
    # A real verdict about a real DOI always wins.
    ("corrected", "no_doi", "W123", False),
    ("not_found", "mismatch", "W123", False),
])
def test_keeps_no_doi(new_status, prior_status, work_id, expected):
    from shared.doi_verify import keeps_no_doi
    assert keeps_no_doi(new_status, prior_status, work_id) is expected


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

        # Negative control: a "verified" verdict changes nothing at all.
        ok = {"doi_o_verification": "verified",
              "doi_o": "10.1016/j.biopsycho.2015.07.014", "evidence_note": ""}
        with patch("extract.run_extract.verify_and_correct", return_value=ok):
            row = _verify_row(self._row())
        assert row["doi_o_verification"] == "verified"
        assert row["link_evidence"] == "existing evidence"
        assert row["pair_id"] == "x"

    def test_mismatch_downgrades_confidence_and_clears_doi(self):
        """A mismatched DOI is registered but describes a DIFFERENT paper, and
        verify_and_correct found no better candidate. Keeping it would send a
        validator to the wrong original and produce a confident-looking but wrong
        url_o, so the DOI (and its derived bibtex) is dropped; the title claim stays."""
        from extract.run_extract import _verify_row
        v = {"doi_o_verification": "mismatch",
             "doi_o": "10.1016/j.biopsycho.2015.07.014", "evidence_note": "DOI mismatch: ..."}
        with patch("extract.run_extract.verify_and_correct", return_value=v):
            row = _verify_row(self._row())
        assert row["link_confidence"] == "low"
        assert row["doi_o"] == ""
        assert row["doi_o_verification"] == "mismatch"

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

        # --doi narrows the run to one row: the other is never even verified.
        with patch("extract.audit_dois.verify_and_correct") as vc:
            vc.return_value = {"doi_o_verification": "verified",
                               "doi_o": "10.1111/psyp.13449", "evidence_note": ""}
            summary = audit_file(path, apply=False, report_path=tmp_path / "report.csv",
                                 only_doi="10.1111/psyp.13707")
        assert vc.call_count == 1
        assert summary["verified"] == 1


class TestOutageIsNotAnAnswer:
    """A partial outage used to be indistinguishable from "nothing matched", and
    "nothing matched" is what makes verify_and_correct write `mismatch` — which
    DELETES the row's doi_o and quarantines it. A registry being unreachable must
    never cost a row its DOI."""

    TITLE, AUTHOR, YEAR = "Emotion word processing in the brain", "Schindler", 2019

    def test_partial_outage_is_unverifiable_and_uncached(self, tmp_path):
        """CrossRef down, OpenAlex up and holding nothing: the empty hit list was
        cached as {"found": false}, freezing one outage into every later run."""
        from shared import doi_verify as dv

        def fake_get(url, **kw):
            if "crossref.org" in url:
                raise RuntimeError("CrossRef is down")
            return _resp(200, {"results": []})

        with patch.object(dv, "DOI_VERIFY_CACHE_DIR", tmp_path), \
             patch.object(dv.time, "sleep", lambda *_: None), \
             _patch_get(side_effect=fake_get):
            hit = dv.resolve_doi_by_metadata(self.TITLE, self.AUTHOR, self.YEAR)

        assert hit is dv.UNVERIFIABLE
        assert not hit          # falsy, so every `if hit:` caller stays conservative
        assert list(tmp_path.glob("*.json")) == []

    def test_both_sources_answering_nothing_is_a_real_miss(self, tmp_path):
        """The other half of the distinction: two answers of "no match" ARE an
        answer, and stay cached so the search is not re-bought."""
        from shared import doi_verify as dv

        def fake_get(url, **kw):
            if "crossref.org" in url:
                return _resp(200, {"message": {"items": []}})
            return _resp(200, {"results": []})

        with patch.object(dv, "DOI_VERIFY_CACHE_DIR", tmp_path), \
             _patch_get(side_effect=fake_get):
            assert dv.resolve_doi_by_metadata(self.TITLE, self.AUTHOR, self.YEAR) is None
        assert len(list(tmp_path.glob("*.json"))) == 1

    def test_unverifiable_search_keeps_the_doi_instead_of_mismatch(self):
        from shared import doi_verify as dv
        registered_elsewhere = {"registered": True, "title": "A different paper",
                                "first_author_surname": "Other", "year": 2001,
                                "type": "journal-article", "source": "crossref"}
        with patch.object(dv, "fetch_doi_metadata", return_value=registered_elsewhere), \
             patch.object(dv, "resolve_doi_by_metadata", return_value=dv.UNVERIFIABLE):
            out = dv.verify_and_correct("10.1111/psyp.13449", self.TITLE,
                                        self.AUTHOR, self.YEAR)
        assert out["doi_o_verification"] == "api_error"
        assert out["doi_o"] == "10.1111/psyp.13449"   # NOT dropped

    def test_unverifiable_search_on_a_blank_doi_is_not_not_found(self):
        from shared import doi_verify as dv
        with patch.object(dv, "resolve_doi_by_metadata", return_value=dv.UNVERIFIABLE):
            out = dv.verify_and_correct("", self.TITLE, self.AUTHOR, self.YEAR)
        assert out["doi_o_verification"] == "api_error"

    def test_unregistered_needs_every_source_to_have_answered(self, tmp_path):
        """CrossRef 404 says CrossRef has not got it — not that the DOI does not
        exist. `registered: false` may only be written when OpenAlex and doi.org
        answered too, and it is never written from an outage."""
        from shared import doi_verify as dv

        def fake_get(url, **kw):
            if "crossref.org" in url:
                return _resp(404, {})
            raise RuntimeError("OpenAlex is down")

        with patch.object(dv, "DOI_VERIFY_CACHE_DIR", tmp_path), \
             patch.object(dv.time, "sleep", lambda *_: None), \
             _patch_get(side_effect=fake_get):
            assert dv.fetch_doi_metadata("10.9999/nope") is None
        assert list(tmp_path.glob("*.json")) == []

    def test_unregistered_is_recorded_when_all_three_answered(self, tmp_path):
        from shared import doi_verify as dv

        def fake_get(url, **kw):
            if "openalex.org" in url:
                return _resp(200, {"results": []})
            return _resp(404, {})       # CrossRef and, below, doi.org

        with patch.object(dv, "DOI_VERIFY_CACHE_DIR", tmp_path), \
             patch.object(dv.time, "sleep", lambda *_: None), \
             _patch_get(side_effect=fake_get):
            meta = dv.fetch_doi_metadata("10.9999/nope")
        assert meta["registered"] is False
        assert len(list(tmp_path.glob("*.json"))) == 1

    def test_a_pre_change_no_match_entry_is_not_read_as_an_answer(self, tmp_path):
        """The old key cached exactly the poisoned case as {"found": false}. Reading
        one back would skip the UNVERIFIABLE path and let `mismatch` delete a doi_o,
        so the key carries a version: the stale entry misses and is recomputed."""
        from shared import doi_verify as dv
        from shared.cache import write_cache
        from shared.utils import cache_key

        legacy = cache_key(f"{self.TITLE}|{self.AUTHOR}|{self.YEAR}|||0_doisearch")
        write_cache(tmp_path, legacy, {"found": False})

        def fake_get(url, **kw):
            if "crossref.org" in url:
                raise RuntimeError("CrossRef is down")
            return _resp(200, {"results": []})

        with patch.object(dv, "DOI_VERIFY_CACHE_DIR", tmp_path), \
             patch.object(dv.time, "sleep", lambda *_: None), \
             _patch_get(side_effect=fake_get) as get:
            hit = dv.resolve_doi_by_metadata(self.TITLE, self.AUTHOR, self.YEAR)

        assert get.called                 # the stale entry did not short-circuit it
        assert hit is dv.UNVERIFIABLE     # ... and the outage is reported as one


# ── the OpenAlex leg goes through the metered client, not raw requests ────────

class TestOpenAlexCallsAreMetered:
    """OpenAlex bills per request and a free-text `search` costs 10x a filter query
    (CLAUDE.md cost table). doi_verify used to call it with a hand-attached key: no
    throttle, no rotation, and a quota refusal read as "no match"."""

    _CR_EMPTY = {"message": {"items": []}}

    def _fake(self, oa_resp):
        def fake_get(url, **kw):
            return _resp(200, self._CR_EMPTY) if "crossref.org" in url else oa_resp(url, kw)
        return fake_get

    def test_the_search_throttles_and_carries_the_key_modules_headers(self):
        from shared.doi_verify import resolve_doi_by_metadata
        seen = {}

        def oa(url, kw):
            seen["headers"] = kw.get("headers")
            return _resp(200, {"results": []})

        with patch("shared.doi_verify.requests.get", side_effect=self._fake(oa)), \
             patch("shared.openalex_client.requests.get", side_effect=self._fake(oa)), \
             patch("shared.openalex_client.oa_headers", return_value={"X-Key": "slot-2"}), \
             patch("shared.openalex_client.throttle") as thr:
            resolve_doi_by_metadata("An Original Study", "Smith", 2010)

        thr.assert_called_with("openalex", ANY)
        assert seen["headers"] == {"X-Key": "slot-2"}

    def test_a_quota_refusal_raises_rather_than_reading_as_no_match(self):
        """The failure mode this replaces: a 429 swallowed into None, which
        verify_and_correct would take as "no replacement exists" and act on."""
        from shared.openalex_client import OpenAlexQuotaExhausted
        from shared.doi_verify import resolve_doi_by_metadata

        def oa(url, kw):
            return _resp(429)

        with patch("shared.doi_verify.requests.get", side_effect=self._fake(oa)), \
             patch("shared.openalex_client.requests.get", side_effect=self._fake(oa)), \
             patch("shared.openalex_client.throttle"), \
             patch("shared.openalex_client.is_budget_refusal", return_value=True), \
             patch("shared.openalex_client.rotate_key", return_value=False):
            with pytest.raises(OpenAlexQuotaExhausted):
                resolve_doi_by_metadata("An Original Study", "Smith", 2010)

    def test_free_text_searches_are_counted_for_the_run_summary(self):
        import shared.openalex_client as oac
        from shared.doi_verify import resolve_doi_by_metadata

        def oa(url, kw):
            return _resp(200, {"results": []})

        before = oac.search_query_count()
        with patch("shared.doi_verify.requests.get", side_effect=self._fake(oa)), \
             patch("shared.openalex_client.requests.get", side_effect=self._fake(oa)), \
             patch("shared.openalex_client.throttle"):
            resolve_doi_by_metadata("An Original Study", "Smith", 2010)
            resolve_doi_by_metadata("Another Original Study", "Jones", 2011)
        assert oac.search_query_count() - before == 2
