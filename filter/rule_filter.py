"""
rule_filter.py — Stage 2 rule-based classifier.

Per RULEBOOK §Filter:
    - Rows from a curated source (``shared.config.CURATED_SOURCES``) skip keyword
      discovery entirely and go to ``needs_review``: a human already listed the
      paper as a replication or reproduction, so phrase matching can only lose it.
    - A paper passes as ``replication`` only when both an explicit replication
      phrase AND a specific author-year citation are present.
    - Vague phrases like "we replicate prior findings on X" become
      ``false_positive`` because no target study is named.
    - When only an exclusion pattern (DNA replication, code/data replication,
      replication fork/origin/stress/timing) fires, the row is ``false_positive``
      with high confidence.
    - When a phrase is present but no author-year cite is found, the row is
      ``needs_review`` for the LLM stage to decide.
    - A bare replication STEM in the TITLE with no phrase anywhere is also
      ``needs_review``: titles are short and topical, so the stem means something
      there that it does not mean inside a long abstract.
    - If only reproduction-flavoured phrases fire, the status is ``reproduction``
      with the same author-year cite gating.

``classify_row`` returns the four FILTER_ADDED_COLS values (filter_status,
filter_method, filter_evidence, filter_confidence) for one candidate row.
``filter_method`` is always ``rule_based`` here; Stage 3 overwrites it to
``screen`` on the rows its front-door screen passes.
"""

from shared.config import CURATED_SOURCES
from shared.utils import non_article_doi, sentence_spans

from filter.phrase_detection import _author_year_cites, keyword_verdict


def classify_row(row: dict) -> dict:
    """Return the four FILTER_ADDED_COLS values for a single candidate row dict."""
    year_val = row.get("year_r")
    try:
        year = int(year_val) if year_val and str(year_val).strip() else None
    except (ValueError, TypeError):
        year = None
    doi = str(row.get("doi_r") or "")

    # Hard-exclude non-article DOIs (figshare data records, peer-review objects) — #17.
    doi_excl = non_article_doi(doi)
    if doi_excl:
        return {
            "filter_status": "false_positive",
            "filter_method": "rule_based",
            "filter_evidence": f"exclusion:{doi_excl}",
            "filter_confidence": "high",
        }

    # Curated-source bypass. A human already put this paper on a replication or
    # reproduction list, so keyword discovery has nothing to add and can only lose
    # it — I4R reproductions are titled "A comment on Smith et al. (2023)" and carry
    # no replication vocabulary. The row goes to Stage 3's screen to be settled.
    # Checked after non_article_doi so a peer-review object on a curated list is
    # still dropped: the bypass trusts the list about the topic, not about the DOI
    # pointing at an article.
    source = str(row.get("source") or "").strip().lower()
    if source in CURATED_SOURCES:
        return {
            "filter_status": "needs_review",
            "filter_method": "rule_based",
            "filter_evidence": f"curated_source:{source}; keyword filter bypassed",
            "filter_confidence": "high",
        }

    # THE keyword decision — the same call Stage 1's snapshot gate makes, so a row
    # Stage 1 admits is exactly a row this function does not call false_positive.
    verdict = keyword_verdict(str(row.get("title_r") or ""),
                              str(row.get("abstract_r") or ""), year)
    text = verdict.text

    if verdict.outcome == "negative":
        return {
            "filter_status": "false_positive",
            "filter_method": "rule_based",
            "filter_evidence": verdict.reason,
            "filter_confidence": "high",
        }

    if verdict.outcome == "ambiguous":
        # Two populations: the #44 exclusion+cite readmission, and a bare replication
        # stem in the TITLE with no phrase anywhere. The latter used to be
        # false_positive here while Stage 1 admitted it — measured at 629 rows per
        # 30,000 of candidates.csv, written by Stage 1 and then skipped by Stage 3
        # (which ignores false_positive rows entirely). They now reach Stage 3,
        # where the cheap pre-screen (shared/prescreen.py, #130) is the tier meant to
        # absorb them before the expensive validated screen.
        return {
            "filter_status": "needs_review",
            "filter_method": "rule_based",
            "filter_evidence": verdict.reason,
            "filter_confidence": "medium",
        }

    phrase, phrase_start = verdict.phrase, verdict.phrase_start
    base_status = "reproduction" if verdict.is_reproduction else "replication"

    # Specific-target gate: at least one author–year citation must be present.
    cited = _author_year_cites(text, year)
    if not cited:
        return {
            "filter_status": "needs_review",
            "filter_method": "rule_based",
            "filter_evidence": f"phrase:{phrase!s}; no author-year cite",
            "filter_confidence": "medium",
        }

    # Proximity gate: the citation must fall in the SAME sentence as the replication
    # phrase. A phrase and a citation that merely co-occur somewhere in the same
    # title+abstract blob are frequently unrelated (e.g. "replication of COVID-19"
    # in a literary-analysis abstract that separately cites an unrelated paper) —
    # this was the confirmed root cause of several false-positive "replication"
    # classifications that produced meaningless downstream outcome=cannot_be_determined
    # rows. See docs/superpowers/specs/2026-07-08-classification-accuracy-fixes-design.md.
    spans = sentence_spans(text)
    sent_span = next(((s, e) for s, e in spans if s <= phrase_start < e), None)
    same_sentence_cites = (
        [c for c in cited if sent_span[0] <= c["start"] < sent_span[1]]
        if sent_span else []
    )
    if not same_sentence_cites:
        return {
            "filter_status": "needs_review",
            "filter_method": "rule_based",
            "filter_evidence": f"phrase:{phrase!s}; no same-sentence cite",
            "filter_confidence": "medium",
        }

    sample_cite = (
        same_sentence_cites[0].get("raw", "")
        if isinstance(same_sentence_cites[0], dict)
        else str(same_sentence_cites[0])
    )
    return {
        "filter_status": base_status,
        "filter_method": "rule_based",
        "filter_evidence": f"phrase:{phrase!s}; cite:{sample_cite}",
        "filter_confidence": "high",
    }
