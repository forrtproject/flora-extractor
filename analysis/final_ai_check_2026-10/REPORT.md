# Final AI check: blind Claude recode of Stage 3 rows (pilot, 2026-10-28)

The unit of FLoRA is the replication–original pair. The check therefore compares works,
not single rows: it pairs the pipeline's originals with the blind coder's one-to-one,
compares each pair, and releases a row only if its work pairs off completely.

## Findings

1. **The check works and costs nothing extra.** Claude Opus 5.5 (`claude-opus-5-5`,
   effort `high`, via `claude -p` on the subscription) coded 12 human-validated rows
   blind. A call took a median of 15 s (range 9–49 s), with about 27k input and 2.3k
   output tokens. At API list prices the 12 calls would have cost $2.86; on the
   subscription the incremental cost is $0.
2. **Agreement with the pipeline.** Type agreed on 12/12 rows. For 9/12 rows, the
   shipped original was paired with an original the blind coder named, and the outcome
   agreed on all 9 pairs. In 6 of the 12 rows' works, the two sides paired off
   completely, so 6 rows would be released.
3. **Of the 7 rows humans judged wrong, the check held 5.** In every case the two sides
   disagreed on which originals the paper re-tests:
   - In three, the shipped and the blind originals did not pair off: two
     genetic-association papers (6 shipped vs 9 blind, 2 vs 6) and a Registered
     Replication Report on psychological distance (3 vs 2). In the last, the blind coder
     omitted Wakslak et al. (2006), and the pipeline had shipped the SSRN version of
     that paper.
   - The other two were abstract-only rows on which the blind coder could not name an
     original at all.
4. **The 2 errors it released are OSF records that validators rejected**
   (`not_validation`): a completed student replication of Nurmsoo and Bloom (2008),
   and a preregistration poster that also reports an initial replication (N = 56).
   Both read as completed replications under the codebook, and the blind coder said
   so. The rejections apply an inclusion policy that the codebook does not state, so a
   blind recode with the same codebook cannot reproduce them.
5. **Two "original wrong" verdicts were sibling reassignments.** In two multi-original
   papers, a validator who saw only one row "corrected" its original to another
   original of the same paper. The pipeline had already shipped that original as its
   own row. Under the pair unit this is not an error of the row, and the check now
   scores it as `sibling`. The outcome verdict given alongside such a correction was
   about the other original, so it is `unscored`. The first of these papers is now
   correct on all fields; the second is undetermined, because its outcome verdict
   cannot be scored.
6. **Released rows: 3 correct, 2 errors, 1 undetermined.** The two errors are the OSF
   rejections. Held rows: 5 errors and 1 correct (Ankyrin). For Ankyrin, the blind
   coder names Baum 2008 and Ferreira 2008 for two originals that the pipeline shipped
   as Schulze 2009 and Lee 2011. Those two rows are not validated, so holding the work
   may well be right.
7. **Quote heuristic.** Every quote from both coders was found in the source text
   (87–100% of its 4-word sequences; the 87% cases differ only in punctuation
   introduced by the parse), so no quote was fabricated. The 9 paired rows graded as
   follows:

   | Verdict | Pairs |
   | --- | ---: |
   | `same` | 5 |
   | `same_claim` | 1 |
   | `adjacent` | 1 |
   | `different` | 2 |

   Both `different` pairs had matching outcome categories: the two coders cited
   different evidence for the same verdict, such as the abstract's summary against a
   sentence in the discussion. Across the pilot, a quote mismatch was never a sign of
   error, so it is best used as a fabrication check and a secondary flag.
8. **Scaling to the ~200 validated records is worthwhile**: it costs $0 and takes about
   20 minutes with 4 parallel calls. With about 100 released rows, a 95% confidence
   interval on precision spans roughly ±4.5 percentage points. That is enough to tell
   95% apart from 85%, but not from 92%.

## Needs your decision

- **The OSF inclusion policy (finding 4).** These are the only errors that got through
  in the pilot. Two ways to handle them:
  - state the policy in the codebook, so that both coders apply it;
  - add a rule-based release filter that holds OSF records for review.

  Which OSF records do validators reject, and why? If you can state the rule, I can
  check it against the rejected records in `sb.json`.

## Decisions to review

- **The release rule holds a whole work when its originals do not pair off**, whichever
  side has the extra one. A different count is the clearest sign of a misread paper. In
  the pilot it held every multi-original work with a human-found outcome error, and one
  correct row. The 207-record run will show how often it holds correct works. An
  original that only the blind coder found could instead be queued for recall while
  the agreeing rows are released.
- **Effort `high`, and the document capped at 150,000 characters with its reference
  list appended.** The parse stores the reference list as its own field. The pipeline
  sends it as a separate keyed block, but my script's first version sent only the body
  text. The first run therefore named only 3 of 12 originals with a title. The list is
  alphabetical, so a numbered citation cannot always be followed to its entry.
- **The blind coder sees no reference list for abstract-only rows.** It therefore cannot
  name an original that the abstract does not cite, and every such row is held. The
  pipeline coded 98 of the 207 validated records without full text, although some of
  them now have a parsed document in the cache. Giving the coder the OpenAlex reference
  list (an input, not the pipeline's answer) would let it name the original on these
  rows.
- **Original pairing** (`original_grade` and `pair_originals` in `final_check.py`).
  Three grades can form a pair:
  - `doi`: equal after `clean_doi()`;
  - `title`: title Jaccard ≥ 0.6, plus agreement on the year or the first author;
  - `author_year`: first author and year agree, but no comparable title.

  Pairs are formed greedily, strongest grade first. `title_only` and `partial` matches
  are reported but cannot form a pair.
- **`failed` vs `mixed` is reported as `soft`** and does not count as agreement. No
  pilot pair differed on that boundary.
- **A blind `prospective` study status counts as a type disagreement**, because a plan
  has no result to validate.
- **Human truth** comes from three sources:
  - `final_*` values, where they exist;
  - rejected records, which count as a type error;
  - otherwise the human checks, used when they agree and recorded as `split` when they
    do not.

  On top of that, a correction naming another shipped original of the same work becomes
  `sibling` (counted correct), and its accompanying outcome verdict becomes `unscored`.
  One pilot row (`validation_inprogress`) rests on a single check.

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
- it marks every row `released` only if the row's pair agrees and the work pairs off
  completely.

`quote_check()` measures:
- how much of each quote is present in the source;
- word overlap and content-word overlap between the two quotes;
- the distance in tokens between the two passages in a shared source;
- the distinctive numbers both quotes share.

It grades each pair `same` / `adjacent` / `same_claim` / `numbers` / `different`, and
adds `+fabricated_*` when a quote is less than half present in the source. `sb.json`
holds no quote fields, so the thresholds rest on the pilot pairs and a synthetic check
that a fabricated quote is flagged.

### Pilot sample

The 12 rows were drawn from the 207 records validated since August
(`work/stage3_removal/tier_features.csv`). Each was joined to the newest `extracted.csv`
on `doi_r` + `doi_o`, and all 207 joined.

The sample was drawn on the raw human verdicts:

| Group | Rows |
| --- | ---: |
| Rejected by validators | 2 |
| Original marked wrong | 4 |
| Outcome marked wrong | 3 |
| Marked correct on all fields | 3 |

Eight of the rows are `llm_title_search` links, and ten had a parsed document. No
validated reproduction was available.

| Record | Link method | Human verdict (pair unit) | Check | Why |
| --- | --- | --- | --- | --- |
| cbd7583d (Nurmsoo & Bloom, OSF) | author_year_search | error: rejected | released | blind coder: completed replication |
| a7126e0b (fast-mapping poster, OSF) | title_search | error: rejected | released | blind coder: reports a result, so completed |
| 429086a5 (hormonal contraception) | title_search | correct (sibling reassignment) | released | 3 shipped and 3 blind originals pair off |
| 55ff05c7 (baldness SNPs) | references | error: original + outcome | held | no full text; 11 shipped vs 1 blind |
| 8fd66ead (norm theory) | title_search | undetermined (sibling; outcome unscored) | released | 2 shipped and 2 blind pair off |
| d61206a5 (psychological distance RRR) | title_search | error: original (SSRN version) | held | 3 shipped vs 2 blind; Wakslak 2006 unpaired |
| 0bfdb9f5 (azoospermia) | references | error: outcome → mixed | held | no full text; no original named |
| b3452ec1 (stroke loci) | title_search | error: outcome → mixed | held | 6 shipped vs 9 blind |
| 2fd9b954 (ulcerative colitis loci) | title_search | error: outcome → successful | held | 2 shipped vs 6 blind |
| 627bd4e4 (Ankyrin) | title_search | correct | held | only 1 of 3 originals pairs |
| 361d9342 (Rand et al. RRR) | title_search | correct | released | — |
| 34f948b0 (temporal framing) | references | correct | released | — |

## Files and rerun

- `final_check.py`: the prompt, the cached `claude -p` call, `pair_originals()`,
  `compare_work()` and `quote_check()`.
- `pilot.py`: builds the human truth (including sibling reassignments), joins records
  to `extracted.csv`, selects the pilot rows, runs the check per work and writes
  `pilot_results.csv`.
- `cache/`: the 12 blind codings. A rerun reads these and makes no new call.

The scripts read `data/` and `work/` from the main checkout. They write only into this
directory and touch no verdict, store or Supabase table.

```bash
FLORA_CACHE_DIR=/home/lukas/flora-extractor/cache \
  /home/lukas/flora-extractor/.venv/bin/python analysis/final_ai_check_2026-10/pilot.py
```
