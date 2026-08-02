"""Tests for shared/utils.py helpers."""
from shared.utils import (bare_work_id, citation_fragment, clean_citation_title,
                          non_article_type, sentence_spans, usable_title)


class TestNonArticleType:
    def test_excluded_types_return_a_reason(self):
        for t in ("dataset", "database", "software", "peer-review",
                  "supplementary-materials", "component", "paratext", "libguides",
                  "grant", "standard"):
            assert non_article_type(t), t

    def test_peer_review_reason_matches_non_article_doi(self):
        """Downstream buckets key off the reason string; keep the two guards aligned."""
        assert non_article_type("peer-review") == "peer_review_object"

    def test_normalises_case_whitespace_and_separators(self):
        assert non_article_type("  DataSet ") == "dataset_record"
        assert non_article_type("supplementary_materials") == "supplementary_material"
        assert non_article_type("PEER_REVIEW") == "peer_review_object"

    def test_missing_or_unknown_types_are_kept(self):
        """Exclude-only: the type field is patchily populated, so anything not
        affirmatively a non-study must pass."""
        for t in ("", None, "   ", "journal-article", "article", "preprint",
                  "book-chapter", "posted-content", "other", "data-paper",
                  "software-paper", "gibberish"):
            assert non_article_type(t) == "", t


class TestBareWorkId:
    def test_strips_openalex_url(self):
        assert bare_work_id("https://openalex.org/W2884670852") == "W2884670852"

    def test_passes_through_bare_id(self):
        assert bare_work_id("W2884670852") == "W2884670852"

    def test_uppercases_and_trims(self):
        assert bare_work_id("  w2884670852/ ") == "W2884670852"

    def test_rejects_non_work_entities(self):
        """Author/source/institution ids share the URL shape but are not work ids."""
        assert bare_work_id("https://openalex.org/A5023888391") == ""
        assert bare_work_id("https://openalex.org/S137773608") == ""

    def test_rejects_junk_and_blank(self):
        assert bare_work_id("") == ""
        assert bare_work_id("10.1037/abc123") == ""
        assert bare_work_id(None) == ""


class TestSentenceSpans:
    def test_single_sentence(self):
        text = "We replicated the effect."
        spans = sentence_spans(text)
        assert spans == [(0, len(text))]

    def test_two_sentences(self):
        text = "First sentence. Second sentence."
        spans = sentence_spans(text)
        assert len(spans) == 2
        assert text[spans[0][0]:spans[0][1]] == "First sentence."
        assert text[spans[1][0]:spans[1][1]] == "Second sentence."

    def test_offsets_align_with_original_text(self):
        """Offsets must index into the ORIGINAL text, not a stripped/masked copy."""
        text = "Intro. We attempted a direct replication of Smith (2010). Discussion."
        spans = sentence_spans(text)
        target = next(s for s in spans if "Smith (2010)" in text[s[0]:s[1]])
        assert text[target[0]:target[1]] == "We attempted a direct replication of Smith (2010)."

    def test_et_al_not_split(self):
        text = "Smith et al. found an effect. The replication failed."
        spans = sentence_spans(text)
        assert len(spans) == 2
        assert "Smith et al. found an effect." in text[spans[0][0]:spans[0][1]]

    def test_initial_not_split(self):
        text = "J. Smith proposed the theory. It was later tested."
        spans = sentence_spans(text)
        assert len(spans) == 2

    def test_empty_text_returns_empty_list(self):
        assert sentence_spans("") == []


class TestCitationTitles:
    """A reference parsed without GROBID structure carries the raw citation line as
    its title. Cleaning it must not mangle titles that only look like one."""

    def test_a_numeric_citation_is_cleaned_down_to_its_title(self):
        raw = ("[2] L.J.T. Balter, et al., Low-grade inflammation decreases emotion "
               "recognition, Brain Behav. Immun. 73 (2018) 216-221.")
        cleaned = clean_citation_title(raw)
        assert cleaned.startswith("Low-grade inflammation decreases emotion recognition")

    def test_a_leading_number_is_only_bibliography_numbering_before_an_author(self):
        """"[12] Angry Men" and "3. Methods…" are titles; the number goes only when
        an author list follows it."""
        for title in ("[12] Angry Men", "3. Methods for Estimating Prevalence",
                      "[18F]FDG uptake in the human brain"):
            assert clean_citation_title(title) == title
            assert usable_title(title)

    def test_a_leading_et_al_is_not_evidence_of_an_author_list(self):
        assert clean_citation_title("Et al., and other stories") == \
            "Et al., and other stories"

    def test_non_latin_and_short_titles_are_titles(self):
        """usable_title() gates confidence and title searches, so a False here would
        follow a record around — it must not fire on a script it cannot spell."""
        assert usable_title("Влияние воспаления на распознавание эмоций")
        assert not citation_fragment("Влияние воспаления на распознавание эмоций")
        assert not citation_fragment("Nudge"), "short, but a title"
        assert not usable_title("Nudge"), "too short to search on"

    def test_citation_fragments_are_recognised(self):
        assert citation_fragment("[3] M. Moieni, M.R")
        assert citation_fragment("M.R"), "what cleaning the fragment leaves behind"
        assert citation_fragment("L.J.T. Balter, et al., Low-grade inflammation")
        assert not usable_title("[3] M. Moieni, M.R")
