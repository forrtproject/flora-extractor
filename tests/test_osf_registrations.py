"""The OSF registration route: the fetcher's text shape and the two specs that read it.

One seam, tested at both ends. `search/fetch_abstracts.py` writes the template
name as the first line of the overlay text; `filter/spec/osf-registration-*.json`
decide the row off that line. The specs cannot reference each other, so the
partition they are written to form — every template-prefixed row claimed by
exactly one of them — is asserted here rather than by the format.

All HTTP is mocked.
"""

from pathlib import Path

import pytest

from filter.engine import backfill
from filter.engine.spec import load_specs
from search import fetch_abstracts as fa
from tests.test_rulebook_v2 import _matches, _row

SPEC_DIR = Path(__file__).resolve().parent.parent / "filter" / "spec"

ADMIT = "osf-registration-completed"
DISCARD = "osf-registration-protocol"


@pytest.fixture(scope="module")
def specs() -> list:
    return load_specs(SPEC_DIR)


def _osf_row(template: str, body: str = "item1: we ran the study") -> dict:
    """A pool row whose abstract is what the backfill would have written."""
    return _row("Registration of a study", f"{fa.OSF_TEMPLATE_PREFIX}{template}\n\n{body}",
                doi="10.17605/OSF.IO/AB12D")


# ---------------------------------------------------------------------------
# The fetcher
# ---------------------------------------------------------------------------


class _Response:
    def __init__(self, payload=None, status_code=200):
        self._payload = payload or {}
        self.status_code = status_code
        self.text = ""

    def json(self):
        return self._payload


def test_the_template_name_leads_the_recovered_text(monkeypatch):
    """The whole point of the source: the template is the first thing a spec sees,
    and the responses form — where OSF keeps the substance — follows it."""
    payload = {"data": {"attributes": {
        "registration_supplement": "Replication Recipe (Brandt et al., 2013): Post-Completion",
        "description": "",
        "registration_responses": {
            "item33": "informative failure to replicate",
            "item29": ["d = 0.06", "CI = [-.218, .340]"],
            "item30": "",
        },
    }}}
    monkeypatch.setattr(fa._SESSION, "get", lambda *a, **k: _Response(payload))

    text, status = fa._fetch_osf_registration("10.17605/OSF.IO/AB12D")
    assert status == "ok"
    assert text.startswith(
        fa.OSF_TEMPLATE_PREFIX + "Replication Recipe (Brandt et al., 2013): Post-Completion\n")
    assert "item33: informative failure to replicate" in text
    assert "item29: d = 0.06; CI = [-.218, .340]" in text   # list answers joined
    assert "item30" not in text                             # empty answers dropped


def test_a_missing_template_still_gets_a_template_line(monkeypatch):
    """So the two specs partition every recovered row: a registration with no
    supplement is claimed by the discard, not by nothing."""
    monkeypatch.setattr(fa._SESSION, "get", lambda *a, **k: _Response(
        {"data": {"attributes": {"registration_responses": {"q": "a"}}}}))
    text, status = fa._fetch_osf_registration("10.17605/OSF.IO/AB12D")
    assert status == "ok"
    assert text.startswith(fa.OSF_TEMPLATE_PREFIX + fa.OSF_TEMPLATE_UNSPECIFIED)


def test_a_project_doi_is_a_definitive_miss_not_a_transient_one(monkeypatch):
    """26 of 60 sampled rows were plain `nodes`, which the registrations endpoint
    404s on. Cached as a miss so no later run re-buys the answer."""
    monkeypatch.setattr(fa._SESSION, "get", lambda *a, **k: _Response(status_code=404))
    assert fa._fetch_osf_registration("10.17605/OSF.IO/AB12D") == (None, "empty")


def test_only_osf_registrant_dois_are_targeted():
    """The endpoint answers about OSF GUIDs and nothing else, so every other DOI
    would be a call whose answer is known before it is made."""
    rows = [{"work_id": 1, "oa": "https://openalex.org/W1", "doi_r": "10.17605/osf.io/ab12d"},
            {"work_id": 2, "oa": "https://openalex.org/W2", "doi_r": "10.1016/j.jesp.2020.1"},
            {"work_id": 3, "oa": "https://openalex.org/W3", "doi_r": ""}]
    assert backfill._osf_targets(rows, set()) == ["10.17605/osf.io/ab12d"]


def test_osf_leads_the_attribution_order_and_ignores_what_others_found():
    """SOURCE_ORDER is the order a recovered abstract is ATTRIBUTED in (not the order
    calls are spent in), and OSF leads it: another source's abstract for the same row
    would otherwise displace the template line. For the same reason the OSF phase is
    the one whose targets are not narrowed by the found-index — a Europe PMC abstract
    for a registration is not a substitute for its template."""
    assert backfill.SOURCE_ORDER[0] == "osf"
    assert backfill._source_shape("osf")["skipped"] == ""   # keyless: never skipped

    rows = [{"work_id": 1, "oa": "https://openalex.org/W1",
             "doi_r": "10.17605/osf.io/ab12d"}]
    found = {"epmc:10.17605/osf.io/ab12d"}
    assert backfill._targets("osf", rows, set(), found) == ["10.17605/osf.io/ab12d"]
    # Every other source does drop a row something already answered with text.
    assert backfill._targets("crossref", rows, set(), found) == []


# ---------------------------------------------------------------------------
# The specs
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("template,body", [
    ("Replication Recipe (Brandt et al., 2013): Post-Completion", "item33: success"),
    # Open-Ended carries nothing on its own, so it needs the intent marker.
    ("Open-Ended Registration", "This is a direct replication of Smith (2009)."),
])
def test_a_completed_registration_is_admitted(specs, template, body):
    row = _osf_row(template, body)
    assert _matches(specs, ADMIT, row)
    assert not _matches(specs, DISCARD, row)


@pytest.mark.parametrize("template,body", [
    ("OSF Preregistration", "We will replicate Smith (2009) in a new sample."),
    ("Replication Recipe (Brandt et al., 2013): Pre-Registration",
     "We aim to replicate the original effect."),
    ("AsPredicted", "Hypothesis: the effect will replicate."),
    ("Registered Report Protocol Preregistration", "We plan to run three studies."),
    ("OSF-Standard Pre-Data Collection", "No data have been collected."),
    # a post-DATA-COLLECTION form is still a registration of a design, not a
    # report of results (maintainer, 2026-08-04) — only post-COMPLETION admits
    ("OSF-Standard Post-Data Collection", "We will analyse the collected data."),
    ("EGAP", "A field experiment in Malawi."),
    # Open-Ended without the marker: nothing says a replication is involved.
    ("Open-Ended Registration", "Materials for an eye-tracking study."),
    (fa.OSF_TEMPLATE_UNSPECIFIED, "Some registration text."),
])
def test_a_protocol_registration_is_discarded(specs, template, body):
    row = _osf_row(template, body)
    assert _matches(specs, DISCARD, row)
    assert not _matches(specs, ADMIT, row)


def test_the_two_rules_partition_every_row_the_backfill_writes(specs):
    """The guard the format cannot give: the discard's `none_of` is a hand-synced
    copy of the admission's `any_of`, so drift between them is what this catches."""
    templates = ["Replication Recipe (Brandt et al., 2013): Post-Completion",
                 "OSF-Standard Post-Data Collection", "Open-Ended Registration",
                 "Registered Report Protocol Preregistration",
                 "OSF Preregistration", "AsPredicted", "EGAP",
                 fa.OSF_TEMPLATE_UNSPECIFIED]
    for template in templates:
        for body in ("a replication of Smith (2009)", "no marker here"):
            row = _osf_row(template, body)
            claimed = [_matches(specs, ADMIT, row), _matches(specs, DISCARD, row)]
            assert sum(claimed) == 1, f"{template!r} / {body!r}: {claimed}"


def test_neither_rule_touches_a_row_the_backfill_did_not_write(specs):
    """The template line is anchored at the start of the ABSTRACT: a paper that
    merely quotes it, or an OSF DOI with an ordinary abstract, is untouched."""
    quoting = _row("Preregistration practices",
                   "Authors write \"OSF registration template: AsPredicted\" in their "
                   "methods sections.", doi="10.17605/OSF.IO/ZZ99Z")
    for row in (_row("A replication study", "We replicated Smith (2009)."), quoting):
        assert not _matches(specs, ADMIT, row)
        assert not _matches(specs, DISCARD, row)


def test_the_prefix_the_fetcher_writes_is_the_prefix_the_specs_read(specs):
    """One contract in two files. If `OSF_TEMPLATE_PREFIX` moves, the specs stop
    firing silently — this is what makes that a test failure instead."""
    spec = next(s for s in specs if s.id == DISCARD)
    assert spec.match.abstract_regex == "^" + fa.OSF_TEMPLATE_PREFIX
    admit = next(s for s in specs if s.id == ADMIT)
    for arm in admit.match.any_of:
        pattern = arm.abstract_regex or arm.all_of[0].abstract_regex
        assert pattern.startswith("^" + fa.OSF_TEMPLATE_PREFIX)
