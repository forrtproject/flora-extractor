"""
Smoke tests for the rule-based filter, against classify_row() — the function
Stage 2 actually calls per row.
"""

import pandas as pd
import pytest

from filter.phrase_detection import (
    find_replication_phrase_span,
    keyword_verdict,
    is_non_scholarly_context,
    is_reproduction_only,
)
from filter.rule_filter import classify_row
from shared.schema import FILTERED_COLS


def test_phrase_detection_positive():
    text = "We replicated the original study by Smith (2010)."
    assert find_replication_phrase_span(text)[0] == "we replicated"


def test_qualifier_phrases_match_plurals():
    """``\\b`` after "replication" fails on the "s" of "replications", so every
    singular-only qualifier pattern silently missed the plural for years.

    Examples deliberately avoid the substring "replications of": the generic
    ``\\breplications? of\\b`` pattern is checked first and would satisfy a weaker
    assertion even if the qualifier patterns were deleted. Asserting the matched
    phrase is what actually pins these patterns.
    """
    for text, want in (
            ("The authors conducted two conceptual replications.", "conceptual replications"),
            ("We report direct replications in a new sample.", "direct replications"),
            ("Three exact replications were preregistered.", "exact replications"),
            ("Two cross-cultural replications followed.", "cross-cultural replications")):
        hit = find_replication_phrase_span(text)
        assert hit is not None, text
        assert hit[0] == want, (text, hit[0])


def test_biological_replication_of_word_order_excluded():
    """BIOLOGICAL only caught "<organism> replication"; virology abstracts using
    the "replication of <organism>" order passed the filter."""
    for text in ("The replication of enteroviruses features low fidelity.",
                 "Restriction of Replication of Oncolytic Herpes Simplex Virus."):
        assert is_non_scholarly_context(text), text


def test_data_availability_boilerplate_excluded():
    text = "Data and code to reproduce the results in this paper are on OSF."
    assert is_non_scholarly_context(text)


def test_reproduction_only():
    text = "We report a computational reproduction of Brown's (2018) original analysis."
    assert find_replication_phrase_span(text) is not None
    assert is_reproduction_only(text)


def test_replication_with_other_phrases_not_flagged_reproduction_only():
    text = "A direct replication of Smith (2010); reproducibility of the result was not the main aim."
    # Both replication and reproduction phrases fire → NOT reproduction-only
    assert find_replication_phrase_span(text) is not None
    assert not is_reproduction_only(text)


def test_find_replication_phrase_span_returns_offsets():
    # Avoids the literal substring "replication of" (checked first in
    # REPLICATION_PHRASES) so the match is deterministically "direct replication".
    text = "Intro sentence. We attempted a direct replication in a new sample of Smith's work (2010)."
    result = find_replication_phrase_span(text)
    assert result is not None
    phrase, start, end = result
    assert phrase == "direct replication"
    assert text[start:end] == "direct replication"


def test_find_replication_phrase_span_none_when_no_phrase():
    assert find_replication_phrase_span("A field experiment on consumer choice.") is None


def _row(title: str, abstract: str, year: int = 2020) -> dict:
    return {
        "doi_r": "10.1/test",
        "title_r": title,
        "abstract_r": abstract,
        "year_r": year,
        "authors_r": "X",
        "journal_r": "J",
        "url_r": "",
        "openalex_id_r": "",
        "source": "openalex",
    }


def test_rule_filter_replication_with_cite():
    out = classify_row(_row(
        "A direct replication of the original effect",
        "We attempted a direct replication of Smith (2010). The results held.",
    ))
    assert out["filter_status"] == "replication"
    assert out["filter_confidence"] == "high"


def test_rule_filter_reproduction_with_cite():
    out = classify_row(_row(
        "Reproducibility study",
        "We ran a computational reproduction of Brown (2018) and found no support.",
    ))
    assert out["filter_status"] == "reproduction"


def test_rule_filter_date_phrase_not_treated_as_cite():
    """A replication phrase plus only a date range (no real author-year cite)
    must fall to needs_review, not auto-accept via a single_bare false match."""
    out = classify_row(_row(
        "A replication study",
        "We attempted to replicate the original effect. "
        "Data were collected between January 2020 and March 2020.",
    ))
    assert out["filter_status"] == "needs_review"
    assert "cite:" not in out["filter_evidence"]


def test_rule_filter_real_cite_still_accepts():
    """A genuine author-year citation still promotes to a high-confidence accept."""
    out = classify_row(_row(
        "A replication study",
        "We attempted a direct replication of Smith (2010) and the effect held.",
    ))
    assert out["filter_status"] == "replication"
    assert out["filter_confidence"] == "high"
    assert "cite:" in out["filter_evidence"]


def test_rule_filter_phrase_no_cite_needs_review():
    out = classify_row(_row(
        "We replicate prior findings",
        "We replicate prior findings in a different population without naming a target study.",
    ))
    assert out["filter_status"] == "needs_review"
    assert out["filter_confidence"] == "medium"


@pytest.mark.parametrize("title,abstract", [
    # biological "replication" — DNA / replication forks
    ("DNA replication mechanisms", "We study DNA replication forks in cells."),
    # technical verb: replicating code, not a study
    ("Software replication",
     "We replicated the code using a public pipeline, no prior study named."),
    # technical object: replicating a dataset
    ("Replication of a dataset",
     "Replication of the dataset using a public repository pipeline."),
])
def test_rule_filter_non_scholarly_excluded(title, abstract):
    """The exclusion patterns fire at phrase level AND make the row a hard
    false_positive — there is no rescuing author-year cite in any of these."""
    assert is_non_scholarly_context(abstract)
    assert find_replication_phrase_span(abstract) is None
    out = classify_row(_row(title, abstract))
    assert out["filter_status"] == "false_positive"
    assert out["filter_evidence"].startswith("exclusion:")


@pytest.mark.parametrize("doi,expected_status,expected_evidence", [
    # #17: figshare DOIs are data records, not articles — reject even if the text
    # reads like a replication with a citation.
    ("10.6084/m9.figshare.4213113.v1", "false_positive", "exclusion:figshare_data_record"),
    # #17: a /reviews/ DOI segment marks a peer-review object, never the study.
    ("10.7287/peerj.10325v0.1/reviews/2", "false_positive", "exclusion:peer_review_object"),
    # negative control: a real article DOI with a genuine replication+cite passes.
    ("10.1037/xge0000123", "replication", None),
])
def test_rule_filter_doi_exclusions(doi, expected_status, expected_evidence):
    row = _row("A direct replication of Smith (2019)",
               "We report a direct replication of Smith (2019).")
    row["doi_r"] = doi
    out = classify_row(row)
    assert out["filter_status"] == expected_status
    if expected_evidence is not None:
        assert out["filter_evidence"] == expected_evidence


def test_rule_filter_exclusion_with_phrase_and_cite_readmitted():
    """#44: an exclusion pattern that misfires on an in-scope computational
    reproduction (phrase + author-year cite both present) is readmitted to
    needs_review for the LLM, not hard-rejected."""
    out = classify_row(_row(
        "Reproducing an analysis",
        "We replicated the code of Smith (2019) exactly and re-ran their analysis.",
    ))
    assert out["filter_status"] == "needs_review"
    assert "phrase+cite present" in out["filter_evidence"]


def test_rule_filter_no_phrase_false_positive():
    out = classify_row(_row(
        "On consumer choice in supermarkets",
        "A field experiment on heuristic decision-making with no replication terminology.",
    ))
    assert out["filter_status"] == "false_positive"


def test_rule_filter_phrase_and_cite_different_sentences_needs_review():
    """Reconstructs the confirmed false-positive pattern (Atwood/Oryx and Crake case):
    a replication-flavored phrase and an unrelated author-year citation appear in
    different sentences, with no topical connection between them."""
    out = classify_row(_row(
        "Merging facts with fiction: replication of COVID-19 in dystopian fiction",
        "This article discusses cross-species transplantation themes in dystopian "
        "fiction. (Glover, 2009) The article by Jayne Glover discusses ecological "
        "philosophy in the same novel.",
        # Note: deliberately avoids the word "viral" next to "replication" here —
        # that would trip the existing BIOLOGICAL exclusion pattern in
        # exclusion-patterns.yaml (viral/virus/dna/... + replication) and return
        # false_positive before the proximity gate is even reached, which is not
        # what this test is checking.
    ))
    assert out["filter_status"] == "needs_review"
    assert out["filter_confidence"] == "medium"
    assert "no same-sentence cite" in out["filter_evidence"]


def test_rule_filter_picks_same_sentence_citation_over_earlier_one():
    """When multiple citations exist, the same-sentence one must be used as sample_cite,
    not simply the first citation found in the whole text."""
    out = classify_row(_row(
        "A study of engineering education",
        "Jones (1999) discussed unrelated background context. "
        "We attempted a direct replication of Smith (2010) in a new sample.",
    ))
    assert out["filter_status"] == "replication"
    assert "smith" in out["filter_evidence"].lower()


def test_rule_filter_row_plus_verdict_fits_the_filtered_schema():
    row = _row("t", "a")
    out = {**row, **classify_row(row)}
    for col in ("filter_status", "filter_method", "filter_evidence", "filter_confidence"):
        assert col in out
    assert list(pd.DataFrame([out]).reindex(columns=FILTERED_COLS).columns) == FILTERED_COLS


def test_curated_source_bypasses_keyword_filter():
    """A curated-list row with no replication vocabulary at all — the I4R comment
    genre — must still reach Stage 3 rather than being dropped as false_positive."""
    row = _row("A comment on Combs et al. (2023)",
               "We examine the effect of anonymous cross-party interaction on polarization.")
    row["source"] = "i4r"
    out = classify_row(row)
    assert out["filter_status"] == "needs_review"
    assert "curated_source:i4r" in out["filter_evidence"]


def test_curated_source_still_drops_non_article_doi():
    """The bypass trusts the list about the topic, not about the DOI: a data record
    on a curated list is still not an article."""
    row = _row("A comment on Smith et al. (2020)", "Some text.")
    row["source"] = "i4r"
    row["doi_r"] = "10.6084/m9.figshare.4213113.v1"
    out = classify_row(row)
    assert out["filter_status"] == "false_positive"
    assert out["filter_evidence"] == "exclusion:figshare_data_record"


def test_uncurated_source_still_keyword_filtered():
    row = _row("A comment on Combs et al. (2023)",
               "We examine the effect of anonymous cross-party interaction.")
    row["source"] = "openalex"
    assert classify_row(row)["filter_status"] == "false_positive"


def test_reanalysis_is_a_reproduction_phrase():
    text = "A re-analysis of Smith (2010) using the original data."
    assert find_replication_phrase_span(text) is not None
    assert is_reproduction_only(text)


def test_computationally_reproducible_matches():
    """The genre says "computationally reproducible" as often as "computational
    reproduction"; the narrower "computational " prefix missed the adverb form."""
    assert find_replication_phrase_span(
        "The main results were computationally reproducible.") is not None


def test_phrase_guard_suppresses_gwas_we_replicated():
    """A GWAS discovery/replication-cohort design is not a study replication."""
    gwas = ("We replicated one SNP (rs133885) from 585 SNPs previously reported "
            "to be associated with general mathematical ability.")
    assert find_replication_phrase_span(gwas) is None


def test_phrase_guard_is_scoped_to_its_phrase():
    """The guard must suppress only its own phrase — another phrase in the same
    text still matches, unlike a row-level exclusion."""
    text = ("We replicated the SNP association across the genome-wide cohort. "
            "This was also a direct replication of Smith (2010).")
    hit = find_replication_phrase_span(text)
    assert hit is not None          # a row-level exclusion would have killed it
    assert hit[0] != "we replicated"  # ... but the guarded phrase is skipped


def test_biological_of_does_not_kill_modifier_collocations():
    """cell/parasite have common non-biological collocations. With a three-word
    filler window the unrestricted BIOLOGICAL_OF made these false_positive, which
    is terminal — they are genuine replications, so the organism word must be the
    HEAD of the phrase, not a modifier."""
    for text in ("A direct replication of the classic cell phone driving study.",
                 "A conceptual replication of the parasite stress theory of values."):
        assert is_non_scholarly_context(text) is None, text
        assert find_replication_phrase_span(text) is not None, text


def test_biological_of_still_excludes_head_position_organisms():
    for text in ("Restriction of Replication of Oncolytic Herpes Simplex Virus.",
                 "The replication of enteroviruses features low fidelity.",
                 "We studied the replication of cells in culture.",
                 "Inhibition of the replication of the parasite in host tissue."):
        assert is_non_scholarly_context(text) == "BIOLOGICAL_OF", text


def test_phrase_guard_is_scoped_to_the_matching_sentence():
    """A guard judges its phrase's own context. Searching the whole title+abstract
    let one stray token anywhere veto every occurrence of the phrase."""
    # GWAS vocabulary in a LATER sentence must not veto the replication claim.
    hit = find_replication_phrase_span(
        "We replicated Smith (2010). We also conducted a GWAS of the sample.")
    assert hit is not None and hit[0] == "we replicated"
    # A later legitimate occurrence survives an earlier guarded one.
    hit = find_replication_phrase_span(
        "We replicated one SNP in the GWAS. In study 2, we replicated Smith (2010).")
    assert hit is not None and hit[0] == "we replicated"
    # The guarded sense itself is still suppressed.
    assert find_replication_phrase_span(
        "We replicated one SNP (rs133885) from 585 SNPs in the genome-wide cohort.") is None


def test_data_availability_requires_an_availability_anchor():
    """Without one it matched ordinary methods prose and made genuine reproductions
    false_positive, which is terminal."""
    for text in ("Data from the published experiment were used to replicate the main result.",
                 "We used the original data to reproduce the findings of Smith (2010).",
                 "Data and code from the original experiment\nWe aim to reproduce the result."):
        assert is_non_scholarly_context(text) is None, text
    for text in ("Data and code are available on OSF to reproduce the results in this paper.",
                 "All materials are deposited in a repository to replicate the analyses."):
        assert is_non_scholarly_context(text) == "DATA_AVAILABILITY", text


# --- #137: vocabulary holes (American reanalysis, negative/passive replicate,
# --- reproduce-side verbs, adverb insertion, reproduction-of results nouns) ---


def test_american_reanalyze_admitted():
    """"reanalyzed"/"reanalyzing" reached neither stage: the token gate has no
    ``reanalyz`` stem and Stage B required "re-analysis OF"."""
    for text in ("Reanalyzing the deworming data.",
                 "We reanalyzed the original trial data.",
                 "A re-analyzed subset of the published cohort."):
        assert find_replication_phrase_span(text) is not None, text
        assert is_reproduction_only(text), text
    # British spelling with "of" — the pre-existing behaviour, unchanged.
    assert find_replication_phrase_span("A re-analysis of Smith (2010).") is not None
    assert find_replication_phrase_span("Re-analyses of the published data.") is not None
    # Near miss: a different re- word that merely starts with "rean".
    assert find_replication_phrase_span("Reanalgesia protocols after surgery.") is None


def test_negative_and_passive_replicate_forms():
    """A failed replication is exactly what FLoRA wants; the list caught only
    "failed to replicate" and "did not replicate"."""
    for text in ("The effect does not replicate in a large preregistered sample.",
                 "The original finding was not replicated.",
                 "These effects were not replicated in either sample.",
                 "The result could not be replicated.",
                 "The association cannot be replicated with the published data.",
                 "We were unable to replicate the anchoring effect.",
                 "Failure to replicate the ego-depletion effect.",
                 "Our inability to replicate the result is discussed."):
        hit = find_replication_phrase_span(text)
        assert hit is not None, text
        assert not is_reproduction_only(text), text
    # Near miss: the negation is genuinely biological, and the row-level
    # exclusion still kills it rather than the new phrase rescuing it.
    bio = "DNA replication was not replicated in the mutant strain."
    assert is_non_scholarly_context(bio) == "BIOLOGICAL"
    assert find_replication_phrase_span(bio) is None


def test_reproduce_side_vocabulary_admitted():
    """Rows saying "reproduce the results" passed Stage A on the ``reproduc``
    token and were then silently dropped by Stage B."""
    for text in ("We reproduce the original results using the authors' data.",
                 "We could not reproduce the reported estimates.",
                 "The authors failed to reproduce the main findings.",
                 "We sought to reproduce the published figures.",
                 "We successfully reproduced the analysis.",
                 "We attempted to reproduce every table in the paper."):
        assert find_replication_phrase_span(text) is not None, text
        assert is_reproduction_only(text), text


def test_perfect_and_never_negations_admitted():
    """"has not BEEN replicated" / "have never been replicated" — the same
    auxiliary+negation shape the arm already covers, but "been" is not "be" and
    "never" is not "not". 803 rows of candidates.csv say it one of those ways."""
    for text in ("The original finding has not been replicated.",
                 "These effects have never been replicated in a preregistered sample.",
                 "The result had not been replicated prior to this study.",
                 "The effect is never replicated outside the lab."):
        hit = find_replication_phrase_span(text)
        assert hit is not None, text
        assert not is_reproduction_only(text), text
    # The pre-existing shapes are unchanged.
    assert find_replication_phrase_span("The original finding was not replicated.") is not None


def test_trying_to_reproduce_admitted():
    """``tri(?:ed|es)|try`` had no "trying"; ``attempt\\w*`` covers its own forms."""
    for text in ("In trying to reproduce the analysis we hit missing data.",
                 "We tried to reproduce the published estimates.",
                 "Anyone who tries to reproduce this will need the raw data."):
        assert find_replication_phrase_span(text) is not None, text
        assert is_reproduction_only(text), text
    # Near miss: "trial to reproduce" is not the phrase, and a bare "trial" must
    # not reach the arm through a stem.
    assert find_replication_phrase_span(
        "A clinical trial of assisted reproduction in older mothers.") is None


def test_reproduce_does_not_fire_on_biological_reproduction():
    """Anchored on a results noun, a first-person subject or a failure framing —
    never on a bare "reproduce"."""
    for text in ("The species reproduces successfully in captivity each spring.",
                 "Cows that reproduce early have a higher lifetime yield.",
                 "Viral genomes reproduce rapidly inside host cells.",
                 "Sexual reproduction of corals under thermal stress."):
        assert find_replication_phrase_span(text) is None, text


def test_adverb_insertion_in_first_person_replicate():
    """"we replicated" missed "we closely/partially/conceptually replicated"."""
    for text in ("We closely replicated the classic paradigm in a new sample.",
                 "We conceptually replicate Smith's original demonstration.",
                 "We partially replicated the reported interaction."):
        hit = find_replication_phrase_span(text)
        assert hit is not None, text
    # The GWAS guard applies to the adverb form exactly as to "we replicated".
    assert find_replication_phrase_span(
        "We directly replicated one SNP from the genome-wide association study.") is None


def test_reproduction_of_results_noun_arm():
    for text in ("A reproduction of the results of Smith (2010).",
                 "CODECHECK certificate: reproduction of the analysis.",
                 "Independent reproductions of the published estimates."):
        assert find_replication_phrase_span(text) is not None, text
        assert is_reproduction_only(text), text
    # Bare "reproduction of" stays unadmitted: measured at 807 exclusive hits per
    # 4.2M rows, dominated by social/biological reproduction and R0.
    for text in ("Reproduction of the social order in schools.",
                 "The Reproduction of Mothering.",
                 "The Work of Art in the Age of Mechanical Reproduction.",
                 "The basic reproduction number of influenza."):
        assert find_replication_phrase_span(text) is None, text


def test_quote_form_reproduction_of_unchanged():
    """The quote-required arm is kept as measured (4 hits, 0 exclusive)."""
    hit = find_replication_phrase_span('A reproduction of "The Stroop Effect" paradigm.')
    assert hit is not None
    assert hit[0] == 'reproduction of "'


def test_biological_abstract_matches_no_new_pattern():
    """Regression: none of the #137 additions fire on ordinary biology."""
    text = ("Viral load and reproduction in host cells. Sexual reproduction of the "
            "parasite was observed under thermal stress, and the organisms reproduce "
            "rapidly. We measured reproductive output and the basic reproduction "
            "number over three seasons.")
    assert find_replication_phrase_span(text, ignore_exclusions=True) is None


# ---------------------------------------------------------------------------
# keyword_verdict — the one keyword decision both stages evaluate
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("title,abstract,outcome", [
    # positive: a precise phrase that survives exclusions and guards
    ("A direct replication of the anchoring effect",
     "We report a direct replication of Smith (2010).", "positive"),
    ("Computational reproduction",
     "We reproduce the results of Brown (2018) from the posted code.", "positive"),
    # ambiguous: a stem in the TITLE only — no phrase anywhere
    ("Reproducibility of the X effect",
     "Bees forage over long distances when the hive is disturbed.", "ambiguous"),
    ("Replicability in the social sciences",
     "We survey how researchers describe their own methods sections.", "ambiguous"),
    # negative: near-misses. A stem in the ABSTRACT alone is not enough — that is
    # the title/abstract distinction the rule now makes explicit.
    ("Foraging patterns in bees",
     "Replicability was not assessed in this observational field study.", "negative"),
    ("Notes on honeybee foraging",
     "Several replications were run independently of the pilot.", "negative"),
    ("A field experiment on consumer choice", "Prices were randomised by store.",
     "negative"),
])
def test_keyword_verdict_tiers(title, abstract, outcome):
    assert keyword_verdict(title, abstract).outcome == outcome


def test_keyword_verdict_exclusion_beats_a_title_stem():
    """A biological title carries the stem too. The exclusion is checked first, so
    "DNA replication" does not enter through the title arm."""
    v = keyword_verdict("Origins of DNA replication in yeast",
                        "We map replication forks and origin firing across the genome.")
    assert v.outcome == "negative"
    assert v.exclusion


def test_keyword_verdict_exclusion_plus_cite_is_ambiguous():
    """#44 rescue: an exclusion fired, but a phrase AND an author-year cite are
    present, so the row is handed on rather than rejected."""
    v = keyword_verdict("A computational reproduction",
                        "We replicated the code of Smith (2019) and re-ran "
                        "every analysis.")
    assert v.outcome == "ambiguous"
    assert v.exclusion and "phrase+cite" in v.reason


def test_keyword_verdict_reports_reproduction_vocabulary():
    v = keyword_verdict("Reproducibility study",
                        "We ran a computational reproduction of Brown (2018).")
    assert v.outcome == "positive" and v.is_reproduction


def test_rule_filter_title_stem_only_reaches_stage_three():
    """Behaviour change: a bare stem in the title used to be false_positive here
    while Stage 1 admitted it, so the row died on arrival at Stage 3. It is now
    needs_review — the population the cheap pre-screen (#130) exists to absorb."""
    out = classify_row(_row(
        "Reproducibility of the X effect",
        "Bees forage over long distances when the hive is disturbed.",
    ))
    assert out["filter_status"] == "needs_review"
    assert out["filter_confidence"] == "medium"
    assert out["filter_evidence"].startswith("title stem:")
