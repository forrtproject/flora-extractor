"""Tests for shared/utils.py helpers, plus the APA reference formatter that is the
only real logic left over from the deleted tests/test_apa_resolver.py."""
import pytest

from analysis.apa_resolver import format_apa_reference
from shared.utils import (bare_work_id, citation_fragment, clean_citation_title,
                          non_article_doi, non_article_title, non_article_type,
                          sentence_spans, usable_title)


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


class TestNonArticleDoi:
    def test_data_repository_deposits_are_excluded(self):
        for doi in ("10.7910/DVN/YFHNRD",                   # Harvard Dataverse
                    "https://doi.org/10.7910/dvn/yfhnrd",   # normalised before matching
                    "10.3886/E116358V1",                    # openICPSR
                    "10.34894/nsjcsb",                      # DataverseNL
                    "10.18710/ls2kux",                      # DataverseNO
                    "10.18170/DVN/0YC8DL",                  # Peking University
                    "10.21979/N9/0RLSDU",                   # DR-NTU (Data)
                    "10.11587/fa9lf5",                      # AUSSDA
                    "10.15139/S3/ZSDNZH",                   # UNC Dataverse
                    "10.18738/T8/UG3TUR",                   # Texas Data Repository
                    "10.2905/jrc.rxdemq8"):                 # EC JRC Data Catalogue
            assert non_article_doi(doi) == "data_repository_deposit", doi

    def test_articles_and_unlisted_prefixes_survive(self):
        for doi in ("10.1037/xge0001234",        # ordinary journal article
                    "10.1177/0956797619830326",
                    "10.1016/j.jesp.2020.104063",
                    "10.5281/zenodo.15527133",   # Zenodo is mixed — see the carve-out
                    "10.32855/dataset.2024.05.023",  # UTA: whole-repository prefix
                    "",
                    None):
            assert non_article_doi(doi) == "", doi

    def test_existing_guards_still_fire(self):
        assert non_article_doi("10.6084/m9.figshare.123") == "figshare_data_record"
        assert non_article_doi("10.7287/peerj.10325v0.1/reviews/2") == "peer_review_object"

    def test_prefix_match_is_whole_prefix_not_a_stem(self):
        """10.791 must not be caught by the 10.7910 entry."""
        assert non_article_doi("10.791/abc123") == ""
        assert non_article_doi("10.79100/abc123") == ""


class TestNonArticleTitle:
    def test_deposit_titles_are_flagged(self):
        for title in (
            "Replication Data for: What Happens When Insurers Make the Insurance Laws?",
            "Vol. 16(2): Replication Data for: America's Two Worlds of Welfare",
            'Replication data for: "Diagnostic Ability and Inappropriate Prescriptions"',
            "  replication data set for the 2019 wave  ",
            "Replication data for Japanese translation of the Oslo questionnaire",
        ):
            assert non_article_title(title) == "data_repository_deposit", title

    def test_near_misses_and_studies_survive(self):
        for title in ("Replication Data Analysis in Psychology",
                      "Replication database of behavioural experiments",
                      "Using replication data for meta-analysis",  # not at the start
                      "A replication data set for X",              # not at the start
                      "Replication of Smith (2009), Study 2",
                      "",
                      None):
            assert non_article_title(title) == "", title

    def test_reproduction_packages_that_are_the_output_survive(self):
        """#137 admits these on purpose; neither guard may sweep them up. CODECHECK
        certificates live on Zenodo/OSF/arXiv, none of which is an excluded prefix."""
        for doi, title in (
            ("10.5281/zenodo.21238766", "CODECHECK Certificate 2025-022"),
            ("10.5281/zenodo.15310766", "CODECHECK Certificate"),
            ("10.7717/peerj-cs.601", "Peer Review #1 of 'GrimoireLab' (v0.1)"),
            ("10.5281/zenodo.7647651",
             "VeRAPAk: Automated Verification and Falsification (Artifact)"),
        ):
            assert non_article_doi(doi) == "", doi
            assert non_article_title(title) == "", title


class TestBareWorkId:
    @pytest.mark.parametrize("raw", [
        "https://openalex.org/W2884670852",
        "W2884670852",
        "  w2884670852/ ",   # uppercased and trimmed
    ])
    def test_accepts_work_ids(self, raw):
        assert bare_work_id(raw) == "W2884670852"

    @pytest.mark.parametrize("raw", [
        # Author/source ids share the URL shape but are not work ids.
        "https://openalex.org/A5023888391",
        "https://openalex.org/S137773608",
        "", "10.1037/abc123", None,
    ])
    def test_rejects_everything_else(self, raw):
        assert bare_work_id(raw) == ""


class TestSentenceSpans:
    def test_offsets_align_with_original_text(self):
        """Offsets must index into the ORIGINAL text, not a stripped/masked copy."""
        text = "Intro. We attempted a direct replication of Smith (2010). Discussion."
        spans = sentence_spans(text)
        target = next(s for s in spans if "Smith (2010)" in text[s[0]:s[1]])
        assert text[target[0]:target[1]] == "We attempted a direct replication of Smith (2010)."

    @pytest.mark.parametrize("text,first", [
        ("Smith et al. found an effect. The replication failed.",
         "Smith et al. found an effect."),
        ("J. Smith proposed the theory. It was later tested.",
         "J. Smith proposed the theory."),
    ])
    def test_abbreviations_do_not_split_a_sentence(self, text, first):
        spans = sentence_spans(text)
        assert len(spans) == 2
        assert first in text[spans[0][0]:spans[0][1]]

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
class TestFormatApaReference:
    """The one surviving test from tests/test_apa_resolver.py.

    That file was deleted because its other tests asserted only the return TYPE of
    loaders reading gitignored production CSVs. This one covers real formatting
    logic — author-list assembly, initials, the ", & " join, and the
    volume(issue), pages tail — and it is the string a human reads when confirming
    a missing-DOI replication, so a silent change to it is worth catching. It lives
    here rather than in its own file because it is the module's only unit-testable
    function.
    """

    def test_full_metadata_renders_every_field(self):
        apa = format_apa_reference({
            "authors": [{"family": "Smith", "given": "Beatrice"},
                        {"family": "Jones", "given": "Alan"}],
            "year": 2023, "title": "A replication study on X",
            "journal": "Journal of Psychology",
            "volume": 45, "issue": 3, "pages": "234-245",
        })
        assert apa == ("Smith, B., & Jones, A. (2023). A replication study on X. "
                       "Journal of Psychology, 45(3), 234-245.")

    def test_single_author_and_no_volume_or_pages(self):
        """One author takes no ampersand, and absent optional fields must be
        omitted rather than rendered as empty punctuation."""
        apa = format_apa_reference({
            "authors": [{"family": "Brown", "given": "Carla"}],
            "year": 2022, "title": "Reproducing X findings", "journal": "Nature",
        })
        assert apa == "Brown, C. (2022). Reproducing X findings. Nature."

    def test_empty_metadata_is_an_empty_string(self):
        assert format_apa_reference({}) == ""
