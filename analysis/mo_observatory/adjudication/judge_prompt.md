You are adjudicating a disagreement between two databases of replication studies. You
will be given one evidence packet about a published replication paper. Two answers,
A and B, are given to one question about it; they come from two different sources, in
random order, and you are not told which is which. Do not try to guess the source.

Your job is to decide, from the paper itself, which answer is right.

Read the packet. If it does not contain the full text, or the full text does not settle
the question, look the paper up through its DOI (publisher page, PubMed / PMC, Europe
PMC, preprint servers) and read the relevant parts. Report what you actually based the
verdict on in `evidence_basis`.

**If the question is "which ORIGINAL study does this paper replicate?"**
- The original is the earlier published study whose finding this paper sets out to
  re-test (or re-analyse), as identified by the paper itself — usually named in the
  introduction or methods ("we replicate X et al., 2009").
- `A` / `B`: that answer names the right original and the other does not.
- `both`: both answers name studies the paper genuinely re-tests (e.g. it replicates
  several originals, or two reports of the same study).
- `neither`: neither is the original; put the one you believe is right in
  `correct_original` (authors, year, title, DOI if known).
- `cannot_tell`: you could not access enough of the paper to decide.
- If an answer lists several originals, judge the list: right if its main entries are
  originals this paper re-tests, even if one is missing.

**If the question is "did the replication SUCCEED?"**
- First code it yourself in `own_outcome`: `success` (the original finding was
  reproduced — same direction and, where the design tests it, statistically supported),
  `failure` (not reproduced — null, or the opposite direction), `inconclusive` (mixed:
  some parts replicated and others not, or results too ambiguous to call), or
  `cannot_tell` (you could not access enough of the paper). Weigh the paper's own
  conclusion but check it against the reported results.
- Then say which of A and B is more defensible in `more_defensible`: `A`, `B`, `both`
  (both are reasonable readings — typically the success/inconclusive boundary on a
  partial replication), `neither`, or `cannot_tell`. "cannot be determined from the
  paper" is only defensible if the paper really does not report a result.

For the question that does not apply, set its field to `n/a`.

Give a short `reasoning` (2–4 sentences, specific to this paper) and a verbatim `quote`
from the paper that carries the decision (empty string if you had no text to quote).
`confidence` is how sure you are of the verdict.
