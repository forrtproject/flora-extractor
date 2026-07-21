"""Tests for shared/utils.py helpers."""
from shared.utils import sentence_spans


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
