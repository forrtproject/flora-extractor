# Final AI check: the pilot behind the release rule (2026-10-28)

This is the evaluation. The pipeline step itself is `extract/final_check.py`, with its
gate in `apply_final_check()` in `extract/export.py`.

The unit of FLoRA is the replication–original pair. The check therefore compares works,
not single rows: it pairs the pipeline's originals with the blind coder's one-to-one,
compares each pair, and releases a row only when three conditions hold:
- its pair agrees on type and outcome;
- the blind coder was confident of that outcome;
- its whole work pairs off, with no original left over on either side.

## Findings (22 human-validated rows, two batches)

1. **The check works and costs nothing extra.** Claude Opus 5.5 (`claude-opus-5-5`,
   effort `high`, via `claude -p` on the subscription) coded each row blind. A call
   took a median of 19 s (range 7–88 s), with about 24k input and 2.1k output tokens.
   At API list prices the first 12 calls would have cost $2.86; on the subscription
   the incremental cost is $0.
2. **12 of the 22 rows were released: 7 correct, 4 errors and 1 undetermined.** The
   check held 8 of the 12 rows humans judged wrong, and 2 of the 9 they judged correct.
   These are the current codings, made on 2026-11-05 under the current quote check.

   | Human verdict (pair unit) | Released | Held |
   | --- | ---: | ---: |
   | correct | 7 | 2 |
   | error | 4 | 8 |
   | undetermined | 1 | 0 |

3. **Three of the four released errors are OSF records that validators rejected or
   doubted, and they are likely validator errors.** The fourth is the directed-forgetting
   paper (finding 6).
   - <https://osf.io/vk6d9>: a replication of Nurmsoo and Bloom (2008, Study 1), rejected
     as `not_validation`.
   - <https://osf.io/cxh8q>: a preregistration poster on fast mapping that also reports
     an initial replication (N = 56). It was rejected, and one validator gave a
     different original (doi:10.1073/pnas.1005238108).
   - <https://osf.io/549eq>: a preregistered replication of Chandler et al. (2012,
     Study 1). One validator rejected it and the other accepted type and original, so
     it is still `need_review`.

   Under the codebook, all three read as completed replications that report a result,
   and the maintainer's reading is that the rejections are likely wrong. No validation
   record has been changed. If all three are corrected, the released rows become 10
   correct, 1 error and 1 undetermined. A blanket hold on OSF records would not separate
   them either, because two other released OSF records are correct.
4. **What held the 8 errors:**
   - In five, the originals did not pair off: three genetic-association papers (6 vs 9,
     2 vs 6, 11 vs 1), a psychological-distance RRR (3 vs 2) and an fMRI study (2 vs 3).
   - In two, the blind coder was not confident of its outcome. Humans corrected both
     outcomes.
   - One, a reply to a commentary, the blind coder called `not_a_replication`; humans
     rejected it as well.
5. **The 2 correct rows held are boundary cases, not faults of the check.**
   - **Ankyrin.** The validated row (Athanasiu 2010) agrees. For the paper's other two
     originals, the blind coder names "the two previous GWA studies" (Baum 2008,
     Ferreira 2008), and the pipeline shipped Schulze 2009 and Lee 2011. Releasing per
     pair instead of per work would release this row, but it would also release two
     errors (stroke loci, reward fMRI), so holding the whole work is the better rule.
   - **Antidepressants and autism/ADHD.** The original found ADHD risk and no autism
     risk; the replication found neither. Under the codebook ("no effect where the
     original also found none is success") the blind coder read this as `mixed` and
     marked itself unconfident. The pipeline and humans said `failed`.
6. **Coding the same prompts twice changed 1 of 22 release decisions, and that one
   released an error.** On the directed-forgetting paper the first coding named one
   original, so the work was held (2 vs 1). The second named both and agreed with the
   pipeline's `failed`, so it was released; humans corrected the outcome to `successful`.
   The confidence flag held two errors and no correct row in both codings, but it is also
   unstable on borderline rows: the fast-mapping poster switched from unconfident to
   confident across the prompt revisions of this pilot, which released it. The rule was
   added post hoc, after batch 2. The 200-record run should code each row twice, to
   measure these flips on a larger sample.
7. **Four fixes during the pilot. Together they took the released rows from 5 correct
   and 2 errors to 7 correct and 3 errors.** The title-matching fix came with batch 2, so
   its effect is already in that count. The metadata reference list then released one
   correct row (representative bureaucracy). The last two fixes were made together and
   cannot be separated: they released one correct row (translation and vocabulary) and
   one error (the fast-mapping poster).
   - **Title matching.** Title similarity now also counts the share of the shorter
     title found in the longer one, so a title cited with its subtitle still matches.
   - **Metadata reference list.** When the document supplies no reference list, the
     coder now gets the work's OpenAlex reference list (OpenCitations as fallback).
     This is the list the pipeline's reference-list rung reads, so it is an input, not
     the pipeline's answer. Of the 97 validated records without a document, 82 have a
     cached list.
   - **Identification from a description.** The codebook's rule for an original that
     the paper only describes ("earlier research found X") was missing from my cut of
     `_TARGET_TASK`. That rule says to name the one entry that fits the description,
     after ruling out siblings. With it restored, the translation paper's original,
     Prince (1996), is named by DOI. Prince's title states the context-versus-
     translation comparison; no other entry does.
   - **Separate confidence fields.** The coder had folded its uncertainty about *which*
     original into `outcome_confident`. The two are now separate:
     - `outcome_confident` covers the verdict only, in the pipeline's own wording;
     - `identified_by` (`citation` / `description`) records how the original was found.

     Identification from a description does not block release.
8. **No quote scored as fabricated under the current check.** A quote counts as present
   only when its 4-word sequences are found in one passage of the source, and each part
   of a `|`-joined quote is scored separately, with the quote taking its worst part. All
   19 paired rows scored at least 0.77 on both sides, against the 0.5 flag, with the
   lowest on the reward fMRI paper. The check runs only on paired rows, so the quotes of
   the 3 unpaired rows are unchecked.
9. **Scaling to the ~200 validated records is worthwhile**: it costs $0 and takes about
   20 minutes with 4 parallel calls (40 minutes coded twice). With about 100 released
   rows, a 95% confidence interval on precision spans roughly ±4.5 percentage points.
   That is enough to tell 95% apart from 85%, but not from 92%.

## Open

- **The OSF records (finding 3).** The rejections are likely validator errors. Someone
  still has to confirm that and correct the three records in the validation tables.

## Decisions to review

- **The release rule holds a whole work when its originals do not pair off**, whichever
  side has the extra one. A different count is the clearest sign of a misread paper.
  The 200-record run will show how often it holds correct works.
- **A confident blind outcome is required for release** (post hoc and unstable;
  finding 6).
- **The metadata reference list is unordered and has no in-text citations.** The blind
  coder can therefore name an original only when the abstract cites it or describes it
  well enough to single out one entry.
- **Effort `high`; the document is capped at 150,000 characters, with its parsed
  reference list appended.** The parse stores the reference list as its own field. The
  pipeline sends it as a separate keyed block, but my script's first version sent only
  the body text, so the first run named only 3 of 12 originals with a title. Over the
  cap, the body is cut and the list kept (up to 50,000 characters). The longest pilot
  document is 128,825 characters, so the cap reached no pilot prompt.
- **Original pairing** (`original_grade` and `pair_originals` in `extract/final_check.py`).
  Title similarity is the larger of the Jaccard index and the share of the shorter
  title's words found in the longer one. Three grades can form a pair:
  - `doi`: equal after `clean_doi()`;
  - `title`: similarity ≥ 0.6, plus agreement on the year or the first author;
  - `author_year`: the first author and the year agree, and the blind coder gave no
    title or one at least 0.3 similar. Two unlike titles by the same author in the
    same year are the sibling case, and do not pair.

  Pairs are formed greedily, strongest grade first. Two pairing choices remain open:
  `author_year` and `title` do not compare two populated, differing DOIs, and the
  shorter-title share lets a short title match any longer one containing its words.
- **`failed` vs `mixed` is reported as `soft`** and does not count as agreement.
- **A blind `prospective` study status counts as a type disagreement.**
- **Human truth** comes from three sources:
  - `final_*` values, where they exist;
  - rejected records, which count as a type error;
  - otherwise the human checks, used when they agree and recorded as `split` when they
    do not.

  On top of that, a correction that names another shipped original of the same work
  becomes `sibling` (counted correct), and its accompanying outcome verdict becomes
  `unscored` unless it was `correct`. That exception is asymmetric, and it affects one
  row: 429086a5 counts as correct. Unscoring it too would make the released rows 6
  correct, 4 errors and 2 undetermined. Several rows are still `validation_inprogress`
  or `need_review`, so their truth rests on one or two checks.

  Two further rules have not been measured: a record
  with `final_type` set but `final_doi_o` or `final_outcome` empty counts that field as
  incorrect unless the shipped field is also blank (then correct), and a validated record whose outcome has since changed in `extracted.csv`
  is joined to the new row and scored against it.
- **Before scaling:** the prompt labels the metadata list "from OpenAlex" even when
  OpenCitations supplied it, and the cache file is not written atomically. The label
  stays until the next prompt revision, because changing it moves the cache key of every
  row that carries the list.

## Method

`extract/final_check.py` builds the prompt from the pipeline's own codebook, imported from
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
response is cached in `cache/final_check/`, keyed on the prompt version, the model and effort, and
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
fields, so the thresholds rest on the pilot pairs and synthetic checks. A quote stitched
from 12 far-apart 10-word phrases of a source scores 0.06 (0.72 under the earlier check),
and a true quote split by a parsed page header scores 0.92.

### Samples

Both batches come from the 207 records validated since August
(`work/stage3_removal/tier_features.csv`). Each was joined to the newest `extracted.csv`
on `doi_r` + `doi_o`.

- **Batch 1 (12 rows)** was chosen for informativeness on the raw human verdicts, taking
  the first rows of each stratum in file order (`.head()`):
  - 2 rejected rows;
  - 4 with the original marked wrong, two of which (55ff05c7, 8fd66ead) also have the
    outcome marked wrong;
  - 3 more with the outcome marked wrong, so 5 rows in all have that verdict;
  - 3 correct on all fields.
- **Batch 2 (10 rows)** is a random draw (seed 2) from the remaining rows: 5 judged
  wrong and 5 judged correct, scored on the pair unit.

The rids in `pilot_results.csv` and `pilot_results_batch2.csv` are the record of these
two samples. `select()` now draws each stratum with a fixed seed from rows sorted by
rid, so it would pick other rows: it serves only new draws (`--draw`), and batch 1 was
not drawn that way. Both batches are enriched for errors (12 of 22 rows judged wrong), so
their rates describe this sample and are not population estimates.

15 of the 22 rows are `llm_title_search` links and 18 had a parsed document. No
validated reproduction was available.

| Record | Batch | Human verdict (pair unit) | Check | Why |
| --- | --- | --- | --- | --- |
| cbd7583d Nurmsoo & Bloom (OSF) | 1 | error: rejected | released | blind coder: completed replication |
| a7126e0b fast-mapping poster (OSF) | 1 | error: rejected | released | blind coder: completed replication |
| 429086a5 hormonal contraception | 1 | correct (sibling reassignment) | released | 3 vs 3 pair off |
| 55ff05c7 baldness SNPs | 1 | error: original + outcome | held | no full text; 11 vs 1 |
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
| b6dad5a7 directed forgetting | 2 | error: outcome → successful | released | 2 vs 2, agrees on `failed`; held as 2 vs 1 in the first coding |
| ed3eae6f reply to Williams | 2 | error: rejected | held | blind coder: `not_a_replication` |
| 3d69a538 reward fMRI (OSF) | 2 | error: rejected | held | 2 vs 3 |
| cadbfc8e religious cognition (OSF) | 2 | correct | released | — |
| dd021dfc antidepressants and autism | 2 | correct | held | `failed` vs unconfident `mixed` |
| e3361013 representative bureaucracy | 2 | correct | released | original named by DOI from the reference list |
| 54c1c5b5 translation and vocabulary | 2 | correct | released | Prince 1996 named from the description, by DOI |
| 80269c6d Wilkins & Kaiser (OSF) | 2 | correct | released | (matching bug, fixed) |

## Files and rerun

- `extract/final_check.py`: the prompt, the cached headless call, `pair_originals()`,
  `compare_work()` and `quote_check()`.
- `pilot.py`: builds the human truth (including sibling reassignments), joins records
  to `extracted.csv`, selects a batch, runs the check per work and writes
  `pilot_results.csv` or `pilot_results_batch2.csv`.
- `cache/final_check/` on the box: the 22 current blind codings, keyed on the prompt
  version, model and effort. The first codings are gone; only the results they gave
  survive, in this report's account of the first run and in git history.

The scripts read `data/`, `work/` and the parse cache from a checkout that holds them.
`pilot.py` writes its results into this directory and its codings to `cache/final_check/`, and touches no verdict, store or Supabase table. Merging
`main` left the prompt version at `58aa27a6c868`, and moving the module left it there too.

To re-score the recorded samples from a checkout on this branch on the box, name the main
checkout. `--cached-only` stops at any miss instead of coding again. Rows are selected by
recorded rid, and a rid that no longer joins stops the run:

```bash
export FLORA_MAIN_CHECKOUT=/home/lukas/flora-extractor FLORA_CACHE_DIR=/home/lukas/flora-extractor/cache
/home/lukas/flora-extractor/.venv/bin/python analysis/final_ai_check_2026-10/pilot.py --batch 1 --cached-only
/home/lukas/flora-extractor/.venv/bin/python analysis/final_ai_check_2026-10/pilot.py --batch 2 --cached-only
```
