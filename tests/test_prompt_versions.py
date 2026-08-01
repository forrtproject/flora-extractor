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
        first = prompt_version("build_classify_prompt")
        prompt_version.cache_clear()
        assert prompt_version("build_classify_prompt") == first

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
        monkeypatch.setattr(prompts, "_TARGET_PROMPT",
                            prompts._TARGET_PROMPT + "\nEXTRA RULE")
        prompt_version.cache_clear()
        after = self._versions()
        assert after["build_target_prompt"] != before["build_target_prompt"]
        assert after["build_classify_prompt"] == before["build_classify_prompt"]

    def test_shared_fragment_edit_changes_every_user(self, monkeypatch):
        """EVIDENCE_POLICY opens most prompts — editing it must invalidate them all."""
        before = self._versions()
        monkeypatch.setattr(prompts, "EVIDENCE_POLICY",
                            prompts.EVIDENCE_POLICY + "Be terse.\n")
        prompt_version.cache_clear()
        after = self._versions()
        changed = {n for n in PROMPT_NAMES if after[n] != before[n]}
        for name in ("build_match_type_prompt",
                     "build_multi_original_prompt", "build_target_prompt",
                     ):
            assert name in changed, f"{name} did not follow EVIDENCE_POLICY"
        # The two outcome prompts state their own evidence policy inline, so they do
        # not follow it either.
        assert "build_outcome_prompt" not in changed
        assert "build_repro_outcome_prompt" not in changed
        # Prompts that do not splice it in are untouched — the front-door screen
        # prompt states its own policy, so it is one of them.
        assert "PDF_REFERENCES_PROMPT" not in changed
        assert "build_classify_prompt" not in changed

    def test_outcome_template_edit_reaches_its_vocabulary_only(self, monkeypatch):
        """One prompt per vocabulary now, and the two are separate documents: an edit
        to the replication body must not invalidate reproduction verdicts."""
        before = self._versions()
        monkeypatch.setattr(prompts, "_OUTCOME_TEMPLATE",
                            prompts._OUTCOME_TEMPLATE + "\n5. ...")
        prompt_version.cache_clear()
        after = self._versions()
        changed = {n for n in PROMPT_NAMES if after[n] != before[n]}
        assert changed == {"build_outcome_prompt"}

    def test_pass_only_fragments_reach_both_vocabularies(self, monkeypatch):
        """The PAPER TEXT block is spliced by both builders, so editing it moves both
        — the fragment is shared even though the bodies are not."""
        before = self._versions()
        monkeypatch.setattr(prompts, "_PAPER_TEXT_BLOCK",
                            prompts._PAPER_TEXT_BLOCK + "\n")
        prompt_version.cache_clear()
        after = self._versions()
        changed = {n for n in PROMPT_NAMES if after[n] != before[n]}
        assert changed == {"build_outcome_prompt", "build_repro_outcome_prompt"}

    def test_json_system_message_edit_changes_every_version(self, monkeypatch):
        """It is spliced into every OpenAI/OpenRouter request at the provider layer,
        so it is part of what the model was asked even though no builder names it."""
        before = self._versions()
        monkeypatch.setattr(prompts, "JSON_SYSTEM_MESSAGE",
                            prompts.JSON_SYSTEM_MESSAGE + " Be brief.")
        prompt_version.cache_clear()
        after = self._versions()
        assert all(after[n] != before[n] for n in PROMPT_NAMES)

    def test_docstrings_are_not_part_of_the_version(self):
        """Canonicalisation strips docstrings and comments: only text that can reach
        the model moves a version."""
        src = prompts._canonical_source(prompts.build_target_prompt)
        assert "Render the target-identification prompt" not in src
        assert "only meaningful against the key_map" not in src
        assert "_TARGET_PROMPT" in src

    def test_prompt_versions_joins_and_follows_each(self, monkeypatch):
        pair = ("build_outcome_prompt", "build_repro_outcome_prompt")
        before = prompt_versions(*pair)
        assert before == "+".join(prompt_version(n) for n in pair)
        monkeypatch.setattr(prompts, "_OUTCOME_TEMPLATE",
                            prompts._OUTCOME_TEMPLATE + "x")
        prompt_version.cache_clear()
        assert prompt_versions(*pair) != before
