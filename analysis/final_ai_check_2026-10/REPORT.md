# Final AI check: blind Claude recode of Stage 3 rows (pilot, 2026-10-28)

The unit of FLoRA is the replication–original pair. The check therefore compares works,
not single rows: it pairs the pipeline's originals with the blind coder's one-to-one,
compares each pair, and releases a row only when three conditions hold:
- its pair agrees on type and outcome;
- the blind coder was confident of that outcome;
- its whole work pairs off, with no original left over on either side.

## Findings (22 human-validated rows, two batches)

1. **The check works and costs nothing extra.** Claude Opus 5.5 (`claude-opus-5-5`,
   effort `high`, via `claude -p` on the subscription) coded each row blind. A call
   took a median of 14 s (range 8–49 s), with about 27k input and 2.3k output tokens.
   At API list prices the first 12 calls would have cost $2.86; on the subscription
   the incremental cost is $0.
2. **9 of the 22 rows were released: 6 correct, 2 errors and 1 undetermined.** The
   check held 10 of the 12 rows humans judged wrong, and 3 of the 9 they judged correct.

   | Human verdict (pair unit) | Released | Held |
   | --- | ---: | ---: |
   | correct | 6 | 3 |
   | error | 2 | 10 |
   | undetermined | 1 | 0 |

3. **Both released errors are OSF projects that humans rejected or doubted.** One is a
   completed student replication of Nurmsoo and Bloom (2008), rejected as
   `not_validation`. The other replicates Chandler et al. (2012); one validator rejected
   it and the other accepted type and original, so the record is still `need_review`.
   Both read as completed replications under the codebook. An unstated inclusion policy
   cannot be reproduced by a blind recode with the same codebook. A blanket hold on OSF
   records would not separate them either: a third released OSF record (the
   religious-cognition replication) is validated as correct.
4. **What held the 10 errors:**
   - In six, the originals did not pair off: three genetic-association papers (6 vs 9,
     2 vs 6, 11 vs 9), a psychological-distance RRR (3 vs 2), a paper where the pipeline
     shipped a background citation as a second original (2 vs 1), and an fMRI study
     (2 vs 3).
   - In three, the blind coder was not confident of its outcome (see finding 6).
   - One, a reply to a commentary, the blind coder called `not_a_replication`; humans
     rejected it as well.
5. **What held the 3 correct rows.** Each hold is a defensible call rather than a
   fault of the check:
   - **Ankyrin.** The validated row (Athanasiu 2010) agrees. For the paper's other two
     originals, the blind coder names "the two previous GWA studies" (Baum 2008,
     Ferreira 2008), and the pipeline shipped Schulze 2009 and Lee 2011. Releasing per
     pair instead of per work would release this row, but it would also release two
     errors (stroke loci, reward fMRI), so holding the whole work is the better rule.
   - **Antidepressants and autism/ADHD.** The original found ADHD risk and no autism
     risk; the replication found neither. Under the codebook ("no effect where the
     original also found none is success") the blind coder read this as `mixed`; the
     pipeline and humans said `failed`. The blind coder marked its own answer
     unconfident. This is a codebook boundary case.
   - **Translation and vocabulary.** The abstract cites only "earlier research", and two
     references fit (Prince 1996; Laufer & Shmueli 1997). The codebook says to name no
     original when two records fit equally well. The pipeline's pick, Prince, was
     right, but it was an inference.
6. **Requiring a confident blind outcome was added after batch 2, so it is
   post hoc.** In batch 2, the released error with a human-corrected outcome
   (Johnson et al., 2002, `failed` → `mixed`) was the one where the blind coder had
   marked its own outcome unconfident. Across both batches, the rule held 3 errors that
   would otherwise have been released, and no correct row. It needs confirming on rows
   it was not chosen on.
7. **Two fixes after batch 2 each released a correct row.**
   - A matching bug: the blind coder gave Wilkins and Kaiser's (2014) title with its
     subtitle, so the title Jaccard fell to 0.5. Title similarity now also counts the
     share of the shorter title found in the longer one.
   - The blind coder now gets the work's OpenAlex reference list (OpenCitations as
     fallback) when the document supplies none. This is the list the pipeline's
     reference-list rung reads, so it is an input, not the pipeline's answer. With it,
     the bureaucracy paper's original is named by DOI, and the azoospermia paper's
     original is now paired. The latter is held for an unconfident outcome, which
     humans corrected. Of the 97 validated records without a document, 82 have a cached
     OpenAlex list; 15 have none from either source.
8. **Quote heuristic.** No quote from either coder was fabricated: every one was found
   in the source text (87–100% of its 4-word sequences; below 100% only through
   punctuation introduced by the parse). Where the two quotes pointed at different
   passages, the outcome category still agreed. A quote mismatch was therefore never a
   sign of error, and the quote check is best used as a fabrication check and a
   secondary flag.
9. **Scaling to the ~200 validated records is worthwhile**: it costs $0 and takes about
   20 minutes with 4 parallel calls, and it is the test the post hoc confidence rule
   needs. With about 100 released rows, a 95% confidence interval on precision spans
   roughly ±4.5 percentage points. That is enough to tell 95% apart from 85%, but not
   from 92%.

## Needs your decision

- **The OSF inclusion policy (finding 3).** These are the only errors that got through.
  Which OSF projects do validators reject, and why? If you can state the rule, I will
  test it against the rejected records in `sb.json`, either as a codebook rule (so both
  coders apply it) or as a release filter.

## Decisions to review

- **The release rule holds a whole work when its originals do not pair off**, whichever
  side has the extra one. A different count is the clearest sign of a misread paper.
  The 200-record run will show how often it holds correct works.
- **A confident blind outcome is required for release** (post hoc; finding 6).
- **The metadata reference list is unordered and has no in-text citations.** The blind
  coder can therefore name an original only when the abstract identifies it well
  enough to pick one entry. Where two entries fit, it names none (translation and
  vocabulary), and the row goes to a human.
- **Effort `high`; the document is capped at 150,000 characters, with its parsed
  reference list appended.** The parse stores the reference list as its own field. The
  pipeline sends it as a separate keyed block, but my script's first version sent only
  the body text, so the first run named only 3 of 12 originals with a title.
- **Original pairing** (`original_grade` and `pair_originals` in `final_check.py`).
  Title similarity is the larger of the Jaccard index and the share of the shorter
  title's words found in the longer one. Three grades can form a pair:
  - `doi`: equal after `clean_doi()`;
  - `title`: similarity ≥ 0.6, plus agreement on the year or the first author;
  - `author_year`: the first author and the year agree, and the blind coder gave no
    title or one at least 0.3 similar. Two unlike titles by the same author in the
    same year are the sibling case, and do not pair.

  Pairs are formed greedily, strongest grade first.
- **`failed` vs `mixed` is reported as `soft`** and does not count as agreement.
- **A blind `prospective` study status counts as a type disagreement.**
- **Human truth** comes from three sources:
  - `final_*` values, where they exist;
  - rejected records, which count as a type error;
  - otherwise the human checks, used when they agree and recorded as `split` when they
    do not.

  On top of that, a correction that names another shipped original of the same work
  becomes `sibling` (counted correct), and its accompanying outcome verdict becomes
  `unscored`. Several rows are still `validation_inprogress` or `need_review`, so their
  truth rests on one or two checks.

## Method

`final_check.py` builds the prompt from the pipeline's own codebook, imported from
`shared/prompts.py`:
- the target rules, cut verbatim out of `_TARGET_TASK`;
- `_OUTCOME_RULES`, `_REPRO_AXIS_RULES` and `_MULTI_TARGET_SCOPE`;
- the outcome vocabulary, from `OUTCOME_LABELS`.

Only the wrapper is new. The record type is asked rather than assumed, and each
original is named by authors, year, title and DOI, because the coder sees no keyed
candidate list. A DOI is accepted only if it is printed in the text.

The evidence is the title, the abstract and the parsed document from the pipeline's
parse cache. The parse is chosen as the ladder chooses it (the best-scoring parse that
has text), and its parsed reference list is appended.

`claude -p` runs with no tools, no MCP servers, no settings and a JSON schema. Each
response is cached in `cache/`, keyed on the prompt version, the model and effort, and
the prompt.

`compare_work()` takes every shipped row of a work and the blind coding of that work:
- it pairs the originals one-to-one (`pair_originals()`);
- for each pair it grades type, outcome and quotes;
- it applies the release rule to every row.

`quote_check()` measures:
- how much of each quote is present in the source;
- word overlap and content-word overlap between the two quotes;
- the distance in tokens between the two passages in a shared source;
- the distinctive numbers both quotes share.

It grades each pair `same` / `adjacent` / `same_claim` / `numbers` / `different`, and
adds `+fabricated_*` when a quote is less than half present. `sb.json` holds no quote
fields, so the thresholds rest on the pilot pairs and a synthetic check that a
fabricated quote is flagged.

### Samples

Both batches come from the 207 records validated since August
(`work/stage3_removal/tier_features.csv`). Each was joined to the newest `extracted.csv`
on `doi_r` + `doi_o`.

- **Batch 1 (12 rows)** was chosen for informativeness on the raw human verdicts:
  - 2 rejected rows;
  - 4 with the original marked wrong;
  - 3 with the outcome marked wrong;
  - 3 correct on all fields.
- **Batch 2 (10 rows)** is a random draw (seed 2) from the remaining rows: 5 judged
  wrong and 5 judged correct, scored on the pair unit.

15 of the 22 rows are `llm_title_search` links and 18 had a parsed document. No
validated reproduction was available.

| Record | Batch | Human verdict (pair unit) | Check | Why |
| --- | --- | --- | --- | --- |
| cbd7583d Nurmsoo & Bloom (OSF) | 1 | error: rejected | released | blind coder: completed replication |
| a7126e0b fast-mapping poster (OSF) | 1 | error: rejected | held | outcome not confident |
| 429086a5 hormonal contraception | 1 | correct (sibling reassignment) | released | 3 vs 3 pair off |
| 55ff05c7 baldness SNPs | 1 | error: original + outcome | held | no full text; 11 vs 9 |
| 8fd66ead norm theory | 1 | undetermined (sibling; outcome unscored) | released | 2 vs 2 pair off |
| d61206a5 psychological distance RRR | 1 | error: original (SSRN version) | held | 3 vs 2 |
| 0bfdb9f5 azoospermia | 1 | error: outcome → mixed | held | outcome not confident |
| b3452ec1 stroke loci | 1 | error: outcome → mixed | held | 6 vs 9 |
| 2fd9b954 ulcerative colitis loci | 1 | error: outcome → successful | held | 2 vs 6 |
| 627bd4e4 Ankyrin | 1 | correct | held | 1 of 3 originals pairs |
| 361d9342 Rand et al. RRR | 1 | correct | released | — |
| 34f948b0 temporal framing | 1 | correct | released | — |
| 7426ac06 Chandler et al. weight (OSF) | 2 | error: one validator rejects | released | blind coder: completed replication |
| 561199ea default effects (OSF) | 2 | error: outcome → mixed | held | outcome not confident |
| b6dad5a7 directed forgetting | 2 | error: outcome → successful | held | 2 vs 1: Sahakyan 2004 is background |
| ed3eae6f reply to Williams | 2 | error: rejected | held | blind coder: `not_a_replication` |
| 3d69a538 reward fMRI (OSF) | 2 | error: rejected | held | 2 vs 3 |
| cadbfc8e religious cognition (OSF) | 2 | correct | released | — |
| dd021dfc antidepressants and autism | 2 | correct | held | `failed` vs unconfident `mixed` |
| e3361013 representative bureaucracy | 2 | correct | released | original named by DOI from the reference list |
| 54c1c5b5 translation and vocabulary | 2 | correct | held | two references fit; none named |
| 80269c6d Wilkins & Kaiser (OSF) | 2 | correct | released | (matching bug, fixed) |

## Files and rerun

- `final_check.py`: the prompt, the cached `claude -p` call, `pair_originals()`,
  `compare_work()` and `quote_check()`.
- `pilot.py`: builds the human truth (including sibling reassignments), joins records
  to `extracted.csv`, selects a batch, runs the check per work and writes
  `pilot_results.csv` or `pilot_results_batch2.csv`.
- `cache/`: the 22 blind codings. A rerun reads these and makes no new call.

The scripts read `data/` and `work/` from the main checkout. They write only into this
directory and touch no verdict, store or Supabase table.

```bash
FLORA_CACHE_DIR=/home/lukas/flora-extractor/cache \
  /home/lukas/flora-extractor/.venv/bin/python analysis/final_ai_check_2026-10/pilot.py --batch 2
```
