"""The two rules `analysis/doi_duplicates.py` writes `filter/spec/aliases.json` from.

One article held under two OpenAlex work ids is routed, claimed and extracted twice
(issue #193). The alias map collapses them — but only where the two records agree they
are the same paper, because 13,172 of the pool's 16,374 shared DOIs sit on records with
nothing to do with each other.
"""

import json
from pathlib import Path

from analysis.doi_duplicates import _agree, _apply, _canonical
from filter.engine.workids import load_aliases

ALIASES = Path(__file__).resolve().parent.parent / "filter" / "spec" / "aliases.json"


def _work(work: int, title: str, repository: bool = False) -> dict:
    return {"work": work, "title": title, "repository": repository}


class TestASharedDoiIsBelievedOnlyWhenTheTitlesAgree:

    def test_the_same_paper_under_two_ids_is_merged(self):
        """The confirmed pair: a KU Leuven deposit beside the Frontiers article."""
        assert _agree([
            _work(2558516342, "Convergence in the bilingual lexicon: A pre-registered "
                              "replication study", repository=True),
            _work(2580794897, "Convergence in the Bilingual Lexicon: A Pre-registered "
                              "Replication of Previous Studies")])

    def test_two_unrelated_papers_under_one_doi_are_left_alone(self):
        """OpenAlex records carrying a DOI that is not theirs. Merging on the
        identifier alone would hide one real paper behind another."""
        assert not _agree([
            _work(1, "Adolescentes y métodos anticonceptivos"),
            _work(2, "Relieving Pressure on the Emergency Department with a New "
                     "Treatment Pathway for Hand Trauma Patients")])


class TestWhichCopyIsCanonical:

    def test_the_published_record_outranks_the_repository_deposit(self):
        """It carries the journal name and the publisher's PDF, and the deposit
        carries a repository name and an OAI identifier."""
        deposit = _work(1, "A study", repository=True)
        published = _work(2, "A study")
        assert _canonical([deposit, published]) is published

    def test_otherwise_the_lowest_work_id_wins(self):
        """Arbitrary, and deterministic — which is what the alias file needs."""
        assert _canonical([_work(9, "A study"), _work(4, "A study")])["work"] == 4


class TestApplyAddsToWhatTheFileAlreadyHolds:
    """The file carries the OSF-guid merges of issue #200 as well as these DOI ones."""

    def _seed(self, tmp_path: Path) -> Path:
        path = tmp_path / "aliases.json"
        path.write_text(json.dumps(
            {"version": 1, "aliases": {"W7070882364": "W2776696688"}}), encoding="utf-8")
        return path

    def test_an_entry_this_script_did_not_derive_survives(self, tmp_path):
        path = self._seed(tmp_path)
        added, total = _apply([("10.1/a", [_work(4, "A study"), _work(9, "A study")])],
                              path)
        assert (added, total) == (1, 2)
        assert load_aliases(path) == {7070882364: 2776696688, 9: 4}

    def test_a_group_the_file_contradicts_is_refused_whole(self, tmp_path):
        """Merging it would chain: `resolve()` follows exactly one hop."""
        path = self._seed(tmp_path)
        added, total = _apply(
            [("10.1/a", [_work(7070882364, "A study"), _work(9, "A study")])], path)
        assert (added, total) == (0, 1)
        assert load_aliases(path) == {7070882364: 2776696688}


def test_the_shipped_alias_map_resolves_in_one_hop():
    """`resolve()` follows exactly one hop, so a canonical id that is itself an alias
    would leave two ids for one work — the thing the file is written to prevent."""
    aliases = load_aliases(ALIASES)
    assert aliases, "the alias map is empty"
    assert not set(aliases.values()) & set(aliases)
    raw = json.loads(ALIASES.read_text(encoding="utf-8"))["aliases"]
    assert raw["W2558516342"] == "W2580794897"
