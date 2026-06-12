"""Live CrossRef/OpenAlex tests for shared/doi_verify.py.

Run with: TEST_LIVE_API=1 python -m pytest tests/live/test_doi_verify_live.py -v
Uses the real failure case from doi_r 10.1111/psyp.13707.
"""
import os

import pytest

pytestmark = pytest.mark.skipif(
    not os.getenv("TEST_LIVE_API"),
    reason="set TEST_LIVE_API=1 to run live API tests",
)


def test_fetch_metadata_real_doi():
    from shared.doi_verify import fetch_doi_metadata
    meta = fetch_doi_metadata("10.1111/psyp.13449")
    assert meta is not None
    assert meta["registered"] is True
    assert meta["year"] in (2019, 2020)


def test_wrong_doi_does_not_match_correct_title():
    from shared.doi_verify import fetch_doi_metadata, metadata_matches
    correct = fetch_doi_metadata("10.1111/psyp.13449")
    wrong   = fetch_doi_metadata("10.1016/j.biopsycho.2015.07.014")
    assert wrong is not None and wrong["registered"]
    # the wrong DOI's own metadata must NOT match the correct paper's title
    assert not metadata_matches(wrong, correct["title"], correct["first_author_surname"],
                                correct["year"])


def test_resolve_corrects_the_real_case():
    from shared.doi_verify import fetch_doi_metadata, resolve_doi_by_metadata
    correct = fetch_doi_metadata("10.1111/psyp.13449")
    hit = resolve_doi_by_metadata(correct["title"], correct["first_author_surname"],
                                  correct["year"])
    assert hit is not None
    assert hit["doi"] == "10.1111/psyp.13449"
