"""Tests for the @key namespace (shared/target_keys.py) and the merged
target-identification prompt (shared/prompts.build_target_prompt).

The keys are the prompt's only way of naming a work, and the pipeline resolves the
model's answer through the key_map from the same call — so a key that collides,
survives deduplication twice, or cannot be looked up is a wrong original waiting to
be written.
"""
from shared.prompts import build_target_prompt
from shared.target_keys import assign_target_keys


def _cand(**over) -> dict:
    base = {"openalex_id": "W1", "doi": "10.1/orig", "title": "The original study",
            "year": 2009, "first_author": "Smith", "all_authors": ["Smith", "Jones"]}
    base.update(over)
    return base


def _ref(**over) -> dict:
    base = {"id": "W1", "doi": "10.1/orig", "title": "The original study",
            "publication_year": 2009,
            "authorships": [{"author": {"display_name": "Jane Smith"}}]}
    base.update(over)
    return base


class TestKeyAssignment:
    def test_key_is_surname_plus_year(self):
        entries, key_map = assign_target_keys([_cand()], [])
        assert entries[0]["key"] == "@smith2009"
        assert key_map["@smith2009"] is entries[0]

    def test_same_work_on_both_sides_is_one_entry(self):
        """Matched on DOI: two numbered lists offered the same paper twice, as if the
        model had two studies to choose between."""
        entries, key_map = assign_target_keys([_cand()], [_ref(doi="https://doi.org/10.1/ORIG")])
        assert len(entries) == 1
        assert entries[0]["in_candidates"] and entries[0]["in_references"]
        assert len(key_map) == 1

    def test_title_and_year_dedup_without_a_doi(self):
        entries, _ = assign_target_keys(
            [_cand(doi="")], [_ref(doi="", title="  The Original  Study!  ")])
        assert len(entries) == 1

    def test_a_different_year_is_a_different_work(self):
        entries, _ = assign_target_keys([_cand(doi="")], [_ref(doi="", publication_year=2011)])
        assert len(entries) == 2

    def test_distinct_records_collide_into_suffixed_keys(self):
        entries, key_map = assign_target_keys(
            [_cand(doi="10.1/a", title="First"), _cand(doi="10.1/b", title="Second"),
             _cand(doi="10.1/c", title="Third")], [])
        assert [e["key"] for e in entries] == ["@smith2009", "@smith2009b", "@smith2009c"]
        assert {key_map[e["key"]]["doi"] for e in entries} == {"10.1/a", "10.1/b", "10.1/c"}

    def test_missing_surname_and_year_still_get_a_key(self):
        entries, key_map = assign_target_keys(
            [], [{"title": "Anonymous work", "publication_year": 1974},
                 {"title": "No year either", "first_author": "Berg"}])
        assert [e["key"] for e in entries] == ["@anon1974", "@berg"]
        assert set(key_map) == {"@anon1974", "@berg"}

    def test_a_parsed_pdf_reference_shape_is_understood(self):
        entries, _ = assign_target_keys(
            [], [{"authors": ["Bartlett, F. C.", "Other, A."], "year": 1932,
                  "title": "Remembering"}])
        assert entries[0]["key"] == "@bartlett1932"
        assert entries[0]["in_references"] and not entries[0]["in_candidates"]

    def test_the_richer_record_wins_the_merge(self):
        entries, _ = assign_target_keys(
            [_cand(doi="", all_authors=["Smith"])],
            [_ref(doi="10.1/orig",
                  authorships=[{"author": {"display_name": "Jane Smith"}},
                               {"author": {"display_name": "Bo Jones"}}])])
        assert entries[0]["doi"] == "10.1/orig"
        assert len(entries[0]["authors"]) == 2


class TestPromptRendering:
    def test_empty_blocks_are_omitted_with_their_headers(self):
        prompt = build_target_prompt("T", "", [])
        assert "ABSTRACT:" not in prompt
        assert "REFERENCE LIST:" not in prompt
        assert "INTRODUCTION:" not in prompt
        assert prompt.endswith("Respond with the JSON object only.")

    def test_abstract_budget_is_3000_characters(self):
        prompt = build_target_prompt("T", "x" * 4000, [])
        assert "x" * 3000 in prompt
        assert "x" * 3001 not in prompt

    def test_pdf_abstract_contributes_only_its_tail(self):
        entries, _ = assign_target_keys([], [])
        prompt = build_target_prompt(
            "T", "The OpenAlex abstract.", entries,
            pdf_abstract="The  OpenAlex\nabstract. And a sentence it lacks.")
        assert prompt.count("The OpenAlex abstract.") == 1
        assert "And a sentence it lacks." in prompt

    def test_a_pdf_abstract_that_adds_nothing_is_dropped(self):
        prompt = build_target_prompt("T", "The OpenAlex abstract.", [],
                                     pdf_abstract="The OpenAlex abstract.")
        assert "ABSTRACT CONTINUED" not in prompt

    def test_the_reference_list_is_never_truncated(self):
        refs = [{"title": f"Reference {i}", "publication_year": 1900 + i,
                 "first_author": f"Author{i}"} for i in range(300)]
        entries, _ = assign_target_keys([], refs)
        prompt = build_target_prompt("T", "A", entries)
        assert all(e["key"] in prompt for e in entries)


class TestDistinctDoisNeverMerge:
    def test_same_title_and_year_but_two_registered_dois_stay_apart(self):
        """Title and year agreeing is evidence of sameness only while one side is
        DOI-less. Merging two registered DOIs makes the first answer for the second,
        so picking the reference would resolve to the candidate's DOI."""
        entries, key_map = assign_target_keys(
            [_cand(doi="10.1/candidate")], [_ref(doi="10.1/reference")])

        assert len(entries) == 2
        assert {e["doi"] for e in entries} == {"10.1/candidate", "10.1/reference"}
        assert [e["key"] for e in entries] == ["@smith2009", "@smith2009b"]
        assert key_map["@smith2009b"]["doi"] == "10.1/reference"

    def test_a_doi_less_duplicate_still_merges_into_the_first_record(self):
        entries, _ = assign_target_keys([_cand(doi="10.1/candidate")], [_ref(doi="")])

        assert len(entries) == 1
        assert entries[0]["doi"] == "10.1/candidate"
        assert entries[0]["in_candidates"] and entries[0]["in_references"]
