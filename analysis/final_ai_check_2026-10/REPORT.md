# Final AI check: blind Claude recode of Stage 3 rows (pilot, 2026-10-28)

## Findings

1. **The check works and costs nothing extra.** Claude Opus 5.5 (`claude-opus-5-5`,
   effort `high`, via `claude -p` on the subscription) coded 12 human-validated rows
   blind. A call took a median of 15 s (range 9–49 s), with about 27k input and 2.3k
   output tokens. At API list prices the 12 calls would have cost $2.86; on the
   subscription the incremental cost is $0.
2. **Field agreement with the pipeline.** Type agreed on 12/12 rows, the row's original
   on 9/12 and the outcome on 12/12. The work's full set of originals agreed on 6/12,
   and 6/12 rows agreed on everything and would be released.
3. **The check flagged 5 of the 9 rows in which humans found an error.** All five were
   flagged because the blind coder and the pipeline disagreed on *which* originals the
   paper re-tests:
   - In three, the two sides listed different numbers of originals: two
     genetic-association papers (6 vs 9 and 2 vs 6) and a Registered Replication Report
     on psychological distance (3 vs 2; the blind coder omitted Wakslak et al., 2006).
   - The other two were abstract-only rows on which the blind coder could not name an
     original at all.
4. **The four human-found errors the check released look like validator conventions
   rather than misreadings.**
   - Two OSF records were rejected by validators (`not_validation`): a completed
     student replication of Nurmsoo and Bloom (2008), and a preregistration poster that
     also reports an initial replication (N = 56). Both read as completed replications
     under the codebook, and the blind coder said so.
   - In two multi-target papers, a validator "corrected" the original to *another*
     original of the same paper. The pipeline had already shipped that original as a
     separate row, and in one case the validator also recoded the paper-level outcome
     as `mixed`.
   Because the check uses the pipeline's own codebook, it cannot catch errors that are
   disagreements between the codebook and validator practice.
5. **The outcome never disagreed, including on the five rows where humans corrected
   the outcome.** Four of those five were flagged anyway, by the target-set check or by
   a missing original. Most of these human corrections are `failed` → `mixed` on
   multi-target papers. Validators code these at the level of the whole paper, whereas
   `_MULTI_TARGET_SCOPE` codes each original separately, and the blind coder follows the
   codebook just as the pipeline does.
6. **Quote heuristic.** Every quote from both coders was found in the source text
   (87–100% of its 4-word sequences; the 87% cases differ only in punctuation
   introduced by the parse), so no quote was fabricated. Of the 11 pairs where both
   coders quoted, the heuristic graded 6 `same`, 1 `adjacent`, 1 `same_claim` and 3
   `different`. All three `different` pairs had matching outcome categories: the two
   coders cited different evidence for the same verdict, such as the abstract's summary
   against a sentence in the discussion. In this pilot, therefore, a quote mismatch was
   not a sign of error. It is best used as a fabrication check and a secondary flag.
7. **Scaling to the ~200 validated records is worthwhile**: it costs $0 and takes about
   20 minutes with 4 parallel calls. Two scoring questions should be settled first,
   because they decide what "precision" means (see below). With about 100 released rows,
   a 95% confidence interval on precision spans roughly ±4.5 percentage points. That is
   enough to tell 95% apart from 85%, but not from 92%.

## Needs your decision

- **How to score the validator-convention cases (finding 4).** The pilot treats the OSF
  rejections and the sibling-original corrections as errors. If they stay errors, the
  set of rows released on full agreement cannot reach 95% precision on this evidence:
  2 of the 6 released pilot rows were fully correct. Two alternatives:
  - make the convention explicit, either in the codebook (so both coders apply it) or
    as a rule-based release filter, for example holding OSF records and
    multi-original works for review;
  - report precision both with and without these cases.
- **Whether full target-set agreement is part of the release rule.** It caught three
  of the four multi-target outcome errors in the pilot, but it also halves the release
  rate, and it
  flagged one fully correct validated row (Ankyrin, where the blind coder names Baum
  2008 and Ferreira 2008 for the two originals the pipeline shipped as Schulze 2009 and
  Lee 2011; those two rows are not validated, so this flag may well be right).

## Decisions to review

- **Effort `high`, and the document capped at 150,000 characters with its reference
  list.** The XML parses keep the reference list outside `raw_text`, so the first run
  could not read titles: only 3 of the 12 originals were named with a title. The
  parsed list is now appended as printed. The list is alphabetical, so a numbered
  citation cannot always be followed to its entry.
- **The blind coder sees no reference list for abstract-only rows.** It therefore
  cannot name an original that the abstract does not cite, so every such row goes to
  a human. The pipeline coded 98 of the 207 validated records without full text,
  although some of them now have a parsed document in the cache. Giving the coder the
  OpenAlex reference list (an input, not the pipeline's answer) would let it name the
  original on these rows.
- **Original-match grades** (`original_grade` in `final_check.py`) count three grades
  as agreement:
  - `doi`: the DOIs are equal after `clean_doi()`;
  - `title`: title Jaccard ≥ 0.6, plus agreement on the year or the first author;
  - `author_year`: the first author and the year agree, but there is no comparable title.

  `title_only` and `partial` are reported but do not count as agreement.
- **`failed` vs `mixed` is reported as `soft`** but is not counted as agreement in
  `all_agree`. No pilot pair differed on that boundary.
- **A blind `prospective` study status counts as a type disagreement**, because a plan
  has no result to validate.
- **Human truth** is taken from `final_*` where it exists. Rejected records count as a
  type error. Otherwise the human checks are used when they agree, and recorded as
  `split` when they do not. One pilot row (`validation_inprogress`) rests on a single
  check.

## Method

`final_check.py` builds the prompt from the pipeline's own codebook, imported from
`shared/prompts.py`:
- the target rules, cut verbatim out of `_TARGET_TASK`;
- `_OUTCOME_RULES`, `_REPRO_AXIS_RULES` and `_MULTI_TARGET_SCOPE`;
- the outcome vocabulary, from `OUTCOME_LABELS`.

Only the wrapper is new. The record type is asked rather than assumed, and the original
is named by authors, year, title and DOI, because the coder sees no keyed candidate
list. A DOI is accepted only if it is printed in the text.

The evidence is the title, the abstract and the parsed document, taken from the
pipeline's parse cache. It is chosen the way the ladder chooses it: the best-scoring
parse that has text.

`claude -p` runs with no tools, no MCP servers, no settings and a JSON schema. Each
response is cached in `cache/`, keyed on the prompt version, the model and effort, and
the prompt.

`compare()` grades three things:
- the type;
- the row's original, against the best-matching blind target;
- the outcome, on that target.

`target_set()` also checks that every original the pipeline shipped for the work
matches a blind target, and that every blind target matches a shipped original.

`quote_check()` measures:
- how much of each quote is present in the source;
- word overlap and content-word overlap between the two quotes;
- the distance in tokens between the two passages in a shared source;
- distinctive numbers the two quotes share.

It grades each pair `same` / `adjacent` / `same_claim` / `numbers` / `different`, and
adds `+fabricated_*` when a quote is less than half present in the source.

`sb.json` holds no quote fields, so the heuristic could not be calibrated against
validator-corrected quotes. Its thresholds rest on the 11 pilot pairs and a synthetic
check that a fabricated quote is flagged.

### Pilot sample

The 12 rows are drawn from the 207 records validated since August
(`work/stage3_removal/tier_features.csv`). Each is joined to the newest `extracted.csv`
on `doi_r` + `doi_o`, and all 207 records joined. The sample contains:
- 2 records rejected by validators;
- 4 with the original marked wrong;
- 3 with the outcome marked wrong;
- 3 marked correct on all fields.

Eight of the 12 are `llm_title_search` rows, and ten had a parsed document. No validated
reproduction was available.

| Record | Link method | Human finding | Blind check | Why |
| --- | --- | --- | --- | --- |
| cbd7583d (Nurmsoo & Bloom, OSF) | author_year_search | rejected | released | blind coder: completed replication |
| a7126e0b (fast-mapping poster, OSF) | title_search | rejected | released | blind coder: reports a result, so completed |
| 429086a5 (hormonal contraception) | title_search | original wrong (sibling) | released | all 3 originals match |
| 55ff05c7 (baldness SNPs) | references | original + outcome wrong | flagged | no full text; 11 vs 1 originals |
| 8fd66ead (norm theory) | title_search | original (sibling) + outcome → mixed | released | both originals match |
| d61206a5 (psychological distance RRR) | title_search | original wrong (SSRN version) | flagged | blind coder omits Wakslak 2006 |
| 0bfdb9f5 (azoospermia) | references | outcome → mixed | flagged | no full text; no original named |
| b3452ec1 (stroke loci) | title_search | outcome → mixed | flagged | 6 vs 9 originals |
| 2fd9b954 (ulcerative colitis loci) | title_search | outcome → successful | flagged | 2 vs 6 originals |
| 627bd4e4 (Ankyrin) | title_search | correct | flagged | blind coder names other originals for 2 of 3 |
| 361d9342 (Rand et al. RRR) | title_search | correct | released | — |
| 34f948b0 (temporal framing) | references | correct | released | — |

## Files and rerun

- `final_check.py`: the prompt, the cached `claude -p` call, `compare()`,
  `target_set()` and `quote_check()`.
- `pilot.py`: builds human truth from `sb.json`, joins records to `extracted.csv`,
  selects the pilot rows, runs the check and writes `pilot_results.csv`.
- `cache/`: the 12 blind codings. A rerun reads these and makes no new call.

The script reads `data/` and `work/` from the main checkout. It writes only into this
directory and touches no verdicts, store or Supabase table.

```bash
FLORA_CACHE_DIR=/home/lukas/flora-extractor/cache \
  /home/lukas/flora-extractor/.venv/bin/python analysis/final_ai_check_2026-10/pilot.py
```
