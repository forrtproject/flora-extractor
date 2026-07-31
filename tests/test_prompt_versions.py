"""Tests for the automatic prompt versioning in shared/prompts.py.

The version of a prompt is derived from its own text plus every fragment it
splices in, and every cache key that can be invalidated by a wording change folds
it in. These tests pin the two properties that make that safe: no prompt can exist
without a version, and a change to a shared fragment reaches every prompt that
uses it.
"""
import pytest

from shared import prompts
from shared.prompts import PROMPT_NAMES, prompt_version, prompt_versions


@pytest.fixture(autouse=True)
def _clear_version_cache():
    prompt_version.cache_clear()
    yield
    prompt_version.cache_clear()


class TestCoverage:
    def test_every_builder_is_versioned(self):
        """Nothing to register: every public build_* function is a prompt by
        construction, so a new one cannot be added unversioned."""
        builders = [n for n in dir(prompts)
                    if n.startswith("build_") and callable(getattr(prompts, n))]
        assert builders
        for name in builders:
            assert name in PROMPT_NAMES, f"{name} is not covered by PROMPT_NAMES"

    def test_standalone_prompt_constants_covered(self):
        assert "PDF_REFERENCES_PROMPT" in PROMPT_NAMES
        assert "PDF_IMAGE_REFERENCES_PROMPT" in PROMPT_NAMES

    def test_all_versions_computable_and_distinct(self):
        versions = {n: prompt_version(n) for n in PROMPT_NAMES}
        assert all(len(v) == 12 for v in versions.values())
        assert len(set(versions.values())) == len(versions)

    def test_version_is_stable_across_calls(self):
        first = prompt_version("build_filter_prompt")
        prompt_version.cache_clear()
        assert prompt_version("build_filter_prompt") == first

    def test_unknown_name_raises(self):
        with pytest.raises(KeyError):
            prompt_version("build_no_such_prompt")

    def test_non_prompt_object_raises(self):
        with pytest.raises(KeyError):
            prompt_version("textwrap")


class TestChangeDetection:
    def _versions(self):
        return {n: prompt_version(n) for n in PROMPT_NAMES}

    def test_template_edit_changes_its_own_version(self, monkeypatch):
        before = self._versions()
        monkeypatch.setattr(prompts, "_IDENT_TEMPLATE",
                            prompts._IDENT_TEMPLATE + "\nEXTRA RULE")
        prompt_version.cache_clear()
        after = self._versions()
        assert after["build_identification_prompt"] != before["build_identification_prompt"]
        assert after["build_filter_prompt"] == before["build_filter_prompt"]

    def test_shared_fragment_edit_changes_every_user(self, monkeypatch):
        """EVIDENCE_POLICY opens most prompts — editing it must invalidate them all."""
        before = self._versions()
        monkeypatch.setattr(prompts, "EVIDENCE_POLICY",
                            prompts.EVIDENCE_POLICY + "Be terse.\n")
        prompt_version.cache_clear()
        after = self._versions()
        changed = {n for n in PROMPT_NAMES if after[n] != before[n]}
        for name in ("build_filter_prompt", "build_match_type_prompt",
                     "build_identification_prompt", "build_multi_original_prompt",
                     "build_classify_prompt", "build_target_prompt",
                     "build_outcome_abstract_prompt", "build_outcome_fulltext_prompt",
                     "build_repro_abstract_prompt", "build_repro_fulltext_prompt"):
            assert name in changed, f"{name} did not follow EVIDENCE_POLICY"
        # A prompt that does not splice it in is untouched.
        assert "PDF_REFERENCES_PROMPT" not in changed

    def test_outcome_rules_edit_reaches_outcome_prompts_only(self, monkeypatch):
        before = self._versions()
        monkeypatch.setattr(prompts, "OUTCOME_RULES", prompts.OUTCOME_RULES + "5. ...\n")
        prompt_version.cache_clear()
        after = self._versions()
        changed = {n for n in PROMPT_NAMES if after[n] != before[n]}
        assert changed == {"build_outcome_abstract_prompt", "build_outcome_fulltext_prompt"}

    def test_quote_instruction_reaches_the_outcome_prompts(self, monkeypatch):
        before = self._versions()
        monkeypatch.setattr(prompts, "QUOTE_INSTRUCTION",
                            prompts.QUOTE_INSTRUCTION.replace("3-6", "2-4"))
        prompt_version.cache_clear()
        after = self._versions()
        changed = {n for n in PROMPT_NAMES if after[n] != before[n]}
        assert changed == {"build_outcome_abstract_prompt", "build_outcome_fulltext_prompt"}

    def test_composed_constants_are_captured_by_value(self, monkeypatch):
        """The reproduction prompts reach QUOTE_INSTRUCTION through REPRO_JSON, which
        is assembled at import. A version is computed from the assembled *value*, so
        editing either source in the file changes it — this pins the value half,
        which a monkeypatch of the upstream fragment cannot reach."""
        before = self._versions()
        monkeypatch.setattr(prompts, "REPRO_JSON_ABSTRACT",
                            prompts.REPRO_JSON_ABSTRACT + " ")
        monkeypatch.setattr(prompts, "REPRO_OUTCOME_RULES",
                            prompts.REPRO_OUTCOME_RULES + "Extra axis note.\n")
        prompt_version.cache_clear()
        after = self._versions()
        changed = {n for n in PROMPT_NAMES if after[n] != before[n]}
        assert changed == {"build_repro_abstract_prompt", "build_repro_fulltext_prompt"}

    def test_docstrings_are_not_part_of_the_version(self):
        """Canonicalisation strips docstrings and comments: only text that can reach
        the model moves a version."""
        src = prompts._canonical_source(prompts.build_identification_prompt)
        assert "Build the LLM identification prompt" not in src
        assert "html_text — extracted landing-page text" not in src
        assert "_IDENT_TEMPLATE.format" in src

    def test_prompt_versions_joins_and_follows_each(self, monkeypatch):
        pair = ("build_outcome_abstract_prompt", "build_outcome_fulltext_prompt")
        before = prompt_versions(*pair)
        assert before == "+".join(prompt_version(n) for n in pair)
        monkeypatch.setattr(prompts, "OUTCOME_RULES", prompts.OUTCOME_RULES + "x")
        prompt_version.cache_clear()
        assert prompt_versions(*pair) != before
