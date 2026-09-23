"""The prompt edits under test, as (old, new) pairs over the RENDERED replication rules.

The same edits, written against the «slot»-marked source, are in fix.patch. Each old
string must occur exactly once in the rendered prefix — `apply()` asserts it, so a
drift in the HEAD text fails loudly rather than replaying an unedited prompt.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
sys.path.insert(0, str(Path(__file__).parent.parent.parent))
import prompts_head as P  # noqa: E402  (HEAD's shared/prompts.py, imports rewritten)

# ── Edit 1: rule 2(a) asks for the FINDING re-tested, not a restatement of it ──
RULE2_OLD = """     (a) the evidence states what THE ORIGINAL THIS OUTCOME IS ABOUT found. You cannot
         compare against
         a finding the evidence never states, however positive this paper's own results
         sound;
     (b) you can name this paper's result that corresponds to it."""
RULE2_NEW = """     (a) you can tell which finding of THE ORIGINAL THIS OUTCOME IS ABOUT was re-tested,
         and which way it pointed. The paper's own statement of the effect, association
         or hypothesis it set out to replicate is enough: it need not restate the
         original's numbers, and need not name the original in the same sentence. You
         cannot compare against a finding the evidence never identifies, however
         positive this paper's own results sound;
     (b) you can name this paper's result that corresponds to it — reported anywhere in
         the evidence, its results included."""

# ── Edit 2: what cbd is NOT for ──
CBD_OLD = """Answer "cannot_be_determined" when the evidence in front of you does not state the outcome.
Do not guess an outcome the evidence does not support, and do not withhold one it does state
(or implies through a comparison as defined in rule 2 above)."""
CBD_NEW = CBD_OLD + """

Before answering "cannot_be_determined", look for the re-test's result in EVERY block you
were given — title, abstract, and any results, discussion or body text — not only in a
closing summary. A result does not have to be phrased as an overall verdict to count:
- A sentence reporting how the re-tested effect came out — "the predicted effect was not
  significant", "as in the original study, X predicted Y", "these findings did not
  replicate those of the original study" — is a comparison under rule 2.
- A title or abstract that states the verdict ("An informative failure to replicate X",
  "we successfully replicated X") is an overall verdict under rule 1.
- A verdict the paper states for part of what it re-tested (one sample, one condition,
  one of the original's effects) decides that part; code the remainder from what the paper
  reports about it, and where it reports nothing about it, code from the part it does.
- Authors who say their own results can neither confirm nor refute the original have given
  the "uninformative" verdict; that is not "cannot_be_determined".
"cannot_be_determined" is for evidence that never reports how the re-test came out, or
reports results that cannot be tied to the original's finding."""

EDITS = [(RULE2_OLD, RULE2_NEW), (CBD_OLD, CBD_NEW)]

# V1b: the same edit without the partial-verdict bullet, which moved cbd-008 (judged
# failure) to mixed under V1.
PARTIAL_BULLET = """
- A verdict the paper states for part of what it re-tested (one sample, one condition,
  one of the original's effects) decides that part; code the remainder from what the paper
  reports about it, and where it reports nothing about it, code from the part it does."""
assert PARTIAL_BULLET in CBD_NEW
EDITS_B = [(RULE2_OLD, RULE2_NEW), (CBD_OLD, CBD_NEW.replace(PARTIAL_BULLET, ""))]


def head_prefix(rtc: bool) -> str:
    p = P.build_target_outcome_prompt("T", "A", [], discussion="d" if rtc else "")
    return p[:p.find("\n\nPAPER\n\n")]


def apply(prefix: str, edits=EDITS) -> str:
    for old, new in edits:
        assert prefix.count(old) == 1, old[:60]
        prefix = prefix.replace(old, new)
    return prefix


FULL_BODY_HEADER = ("THE PAPER, as parsed from the document (its own sections, in "
                    "order, including where each result is reported):\n")
FULL_BODY_CHARS = P.TARGET_FULL_BODY_CHARS


# ── Step 5 (separate, policy): the mixed threshold ──
MIXED_OLD = """That holds even
  where the paper opens by confirming the effect it set out to test: confirming the tested
  effect is one comparison, not a verdict on the whole replication, and only an overall
  verdict that speaks to the replication as a whole (rule 1) outweighs a stated contrast. A
  count of how many measures replicated is not by itself such a mark."""
MIXED_NEW = """That holds even
  where the paper opens by confirming the effect it set out to test: confirming the tested
  effect is one comparison, not a verdict on the whole replication, and only an overall
  verdict that speaks to the replication as a whole (rule 1) outweighs a stated contrast. A
  count of how many measures replicated is not by itself such a mark.
  Both sides of a mixed verdict must be findings OF THE ORIGINAL. A hypothesis, measure,
  condition or population this paper ADDED as an extension is not part of the comparison,
  however it came out. And the differing result must be one the original reported as a
  finding in its own right — a main effect, an interaction, one of several original
  studies or effects. A difference in effect size or numerical detail, or on an auxiliary
  measure (a manipulation check, a mechanism or process measure, a single subscale), while
  the central finding replicated, is successful — and, mirrored, failed."""
CENTRAL_OLD = """- Code the central finding the replication was designed to test. Robustness checks and
  exploratory analyses around it may be left out of the verdict; a tested finding the paper
  itself contrasts with the original may not."""
CENTRAL_NEW = CENTRAL_OLD + """ Extensions — hypotheses, measures or conditions the
  original did not test — are left out of the verdict."""
MIXED_EDITS = [(MIXED_OLD, MIXED_NEW), (CENTRAL_OLD, CENTRAL_NEW)]
