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
        with patch("shared.doi_verify.requests.get", return_value=_resp(404)):
            meta = fetch_doi_metadata("10.9999/does.not.exist")
        assert meta["registered"] is False

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
             patch("extract.run_extract._fetch_ref_o", return_value="new ref"):
            row = _verify_row(self._row())
        assert row["doi_o"] == "10.1111/psyp.13449"
        assert row["doi_o_verification"] == "corrected"
        assert row["pair_id"] == make_pair_id("10.1111/psyp.13707", "10.1111/psyp.13449")
        assert row["ref_o"] == "new ref"
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
             patch("extract.audit_dois._fetch_ref_o", return_value="ref"):
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
             patch("extract.audit_dois._fetch_ref_o", return_value="ref"):
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
