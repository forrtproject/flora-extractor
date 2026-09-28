"""The --no-llm path through the outcome coder.

The CSV runner's flags (--extracted-test, --fresh, --outcome-only) are gone with the
runner: the sandbox is `extract.tier --mode validation` plus
`extract.export --mode validation`, and resume is the verdict checkpoint.
"""
from unittest.mock import patch

from extract.code_outcome import extract_outcome


class TestNoLlmExtractOutcome:
    def test_no_llm_skips_llm_and_returns_cannot_be_determined_when_no_keyword(self):
        """With no_llm=True and no keyword match, returns cannot_be_determined without calling LLM."""
        with patch("extract.code_outcome.call_model") as mock_llm:
            result = extract_outcome(
                "10.1234/test",
                abstract_r="We conducted a study across multiple labs.",
                fulltext="",
                title_r="Multi-site study",
                no_llm=True,
            )
        mock_llm.assert_not_called()
        assert result["outcome"] == "cannot_be_determined"
        assert result["out_quote_source"] == ""

    def test_no_llm_does_not_code_from_a_keyword_hit(self):
        """`--no-llm` leaves the outcome undetermined even where a pattern matches
        cleanly, which this abstract does. Coding it was the old behaviour; the scan
        codes whichever sentence a pattern lands in, and over 214 works that was wrong
        6 times out of 6 at confidence "high" (KEYWORD_OUTCOME_FALLBACK)."""
        result = extract_outcome(
            "10.1234/test2",
            abstract_r="We failed to replicate the original finding.",
            fulltext="",
            title_r="Test",
            no_llm=True,
        )
        assert result["outcome"] == "cannot_be_determined"
        assert result["llm_model"] == ""
