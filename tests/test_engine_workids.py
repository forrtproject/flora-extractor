"""Work identity: the alias map's resolver, and the OSF-guid derivation that feeds it.

`analysis/build_osf_aliases.py` decides which of several OpenAlex works naming one OSF
guid the others alias to, and which groups must not be merged at all. A wrong merge
fuses two studies and nothing downstream says so, so the canonical rule, the refusals
and the file-level invariants are pinned here. The pool is never read: every function
under test takes plain `{work, doi, title}` dicts.
"""

import json

import pytest

from analysis.build_osf_aliases import (
    build_aliases,
    canonical,
    exclusion_reason,
    fragment,
    is_score_label_rename,
    title_fingerprint,
)
from filter.engine.workids import load_aliases, resolve, work_id
from shared.pdf_sources import osf_registration_guid


def _work(wid: int, doi: str = "", title: str = "A replication of Smith (2009)") -> dict:
    return {"work": wid, "doi": doi, "title": title}


# ---------------------------------------------------------------------------
# The canonical pick
# ---------------------------------------------------------------------------


def test_a_published_article_beats_both_osf_dois():
    """The brief's paired example, with the article added: the published DOI wins."""
    group = [_work(6962839798, "10.17605/osf.io/bjmyx"),
             _work(4230164836, "10.31234/osf.io/bjmyx"),
             _work(4409657269, "10.31234/osf.io/bjmyx_v1"),
             _work(9000000001, "10.1037/pspa0000123")]
    assert canonical(group)["work"] == 9000000001


def test_the_registration_doi_beats_a_preprint_doi_and_a_url_only_record():
    """The brief's paired example as it stands: 10.17605 over 10.31234 over no DOI."""
    group = [_work(6962839798, "10.17605/osf.io/bjmyx"),
             _work(4230164836, "10.31234/osf.io/bjmyx"),
             _work(4409657269, "10.31234/osf.io/bjmyx_v1"),
             _work(1000000000)]
    assert canonical(group)["work"] == 6962839798
    assert canonical(group[1:])["work"] == 4230164836


def test_the_tie_break_within_a_tier_is_the_lowest_work_id():
    group = [_work(7110500188), _work(2776696688), _work(7070882364)]
    assert canonical(group)["work"] == 2776696688
    both = [_work(7131872807, "10.17605/osf.io/hpgvj"),
            _work(3000000000, "10.17605/osf.io/hpgvj")]
    assert canonical(both)["work"] == 3000000000


def test_the_version_suffix_collapses_into_one_guid():
    """Grouping is `osf_registration_guid()`'s answer, and it drops `_vN`."""
    assert {osf_registration_guid(f"10.31235/osf.io/d3x9p_v{n}") for n in (1, 2, 3, 4)} \
        == {"d3x9p"}
    assert osf_registration_guid("https://osf.io/d3x9p_v4") == "d3x9p"


# ---------------------------------------------------------------------------
# The refusals
# ---------------------------------------------------------------------------


def test_a_group_larger_than_the_maximum_is_refused_and_the_maximum_is_overridable():
    group = [_work(1000 + n) for n in range(13)]
    assert exclusion_reason("qp4h8", group) == "oversized"
    assert exclusion_reason("qp4h8", group, max_group_size=13) is None


def test_a_score_label_rename_is_merged_and_is_the_one_exemption():
    """The 2024-01-10 SCORE rename: one node retitled, adjudicated MERGE for all 39."""
    group = [_work(6999172155, title="Carrillo_Vega_covid_wxQZ - Cheng/Méndez - "
                                     "Secondary Data Replication - 7mmg"),
             _work(7008415331, title="Carrillo_Vega_covid_wxQZ - Cheng/Méndez - "
                                     "Data Analytic Replication - 7mmg")]
    assert is_score_label_rename(group)
    assert exclusion_reason("unlisted", group) is None
    # The same shape with a title that differs elsewhere is not the rename.
    other = group[:1] + [_work(7008415331, title="Some other study - "
                                                 "Data Analytic Replication - 7mmg")]
    assert not is_score_label_rename(other)
    assert exclusion_reason("unlisted", other) == "unanchored_title_disagreement"
    # …unless the guid is one of the 39 read against OSF: `4rxgz` was retitled
    # "Data Analytic Replication (ML)" → "Secondary Data Replication", which the pair
    # test alone does not recognise.
    assert exclusion_reason("4rxgz", other) is None


def test_entity_and_punctuation_trivia_is_not_a_title_difference():
    """Two harvests of one page differ in escaping and punctuation, not in identity."""
    group = [_work(4000000001, "10.1093/jos/ffz022",
                   "Do children interpret &amp;amp;lsquo;or&amp;amp;rsquo; conjunctively?"),
             _work(4000000002, title="Do children interpret 'or' conjunctively")]
    assert len({title_fingerprint(w["title"]) for w in group}) == 1
    assert exclusion_reason("2srxk", group) is None


def test_an_odd_title_out_that_is_not_doi_anchored_on_the_guid_is_refused():
    """`v2bfn`'s shape: a sibling OSF component pulled in by a stale location URL."""
    group = [_work(7005509803, title="Raw data from the blink experiments 2019/2020"),
             _work(6900000001, title="Raw data from the blink experiments 2021/2022"),
             _work(6900000002, title="Raw data from the blink experiments 2021/2022")]
    assert exclusion_reason("v2bfn", group) == "unanchored_title_disagreement"
    # The same disagreement on a member whose own DOI names the guid is a retitle:
    # OpenAlex cannot have mis-attributed an identifier the record carries itself.
    anchored = [_work(7005509803, "10.17605/osf.io/v2bfn",
                      "Raw data from the blink experiments 2019/2020")] + group[1:]
    assert exclusion_reason("v2bfn", anchored) is None
    # And a version-suffixed preprint DOI anchors just as well.
    versioned = [_work(7005509803, "10.31235/osf.io/v2bfn_v2",
                       "Raw data from the blink experiments 2019/2020")] + group[1:]
    assert exclusion_reason("v2bfn", versioned) is None


# ---------------------------------------------------------------------------
# The fragment and its invariants
# ---------------------------------------------------------------------------


def test_the_fragment_maps_every_member_to_the_canonical_and_never_itself():
    groups = {"qp4h8": [_work(2776696688), _work(7070882364), _work(7110500188)]}
    aliases, conflicts = build_aliases(groups, {})
    assert conflicts == {}
    assert aliases == {7070882364: 2776696688, 7110500188: 2776696688}
    assert not set(aliases) & set(aliases.values())


def test_a_group_that_would_contradict_or_chain_the_existing_map_is_dropped_whole():
    group = [_work(100), _work(200), _work(300)]
    # Our canonical is already someone else's alias — merging would build a chain.
    _, conflicts = build_aliases({"g": group}, {100: 999})
    assert list(conflicts) == ["g"]
    # A member is already aliased elsewhere.
    _, conflicts = build_aliases({"g": group}, {200: 999})
    assert list(conflicts) == ["g"]
    # A member is already another group's canonical.
    _, conflicts = build_aliases({"g": group}, {999: 200})
    assert list(conflicts) == ["g"]
    # An entry the file already holds is not a conflict, and is not re-emitted.
    aliases, conflicts = build_aliases({"g": group}, {200: 100})
    assert conflicts == {} and aliases == {300: 100}


def test_the_fragment_is_written_the_way_aliases_json_spells_ids(tmp_path):
    """W-prefixed strings, as the file has them: `alias_release` reads the spelling."""
    path = tmp_path / "fragment.json"
    path.write_text(json.dumps(fragment({7070882364: 2776696688})), encoding="utf-8")
    assert json.loads(path.read_text())["aliases"] == {"W7070882364": "W2776696688"}
    assert load_aliases(path) == {7070882364: 2776696688}
    assert resolve(7070882364, load_aliases(path)) == 2776696688
    assert resolve(2776696688, load_aliases(path)) == 2776696688


def test_work_id_accepts_the_three_spellings_and_refuses_anything_else():
    assert work_id("https://openalex.org/W123") == work_id("W123") == work_id("123")
    with pytest.raises(ValueError):
        work_id("osf.io/qp4h8")
