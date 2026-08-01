# Stage 3: Extract — Code Flow

**Entry point:** `python -m extract.run_extract`

## What it does

For each row of `filtered.csv` that Stage 2 accepted, Stage 3 answers two questions:

1. Which original study does this paper target (`doi_o`, `title_o`, `link_method`)?
2. What was the result (`outcome`, `outcome_phrase`)?

Rows are appended to `data/extracted.csv` one at a time, so the monitoring app can be
opened while a run is still going. Every row passes DOI verification and
`oa_work_id_*` stamping on the way out, and `extract/sanity_check.py` runs at the end
of every invocation — on normal completion and on Ctrl-C.

The whole stage is organised around spending the cheap calls first. The classification
screen discards 58% of the rows that reach it, the resolution ladder returns at the
first rung that resolves, and the outcome LLM — the most expensive call, because it can
escalate to 8,000 characters of full text — runs only on a row that already has a
confirmed original.

## Per-row flow

```
run_extract.py
    │
    ├── load filtered.csv, apply --year / --source / --doi-r / --predicted-outcome filters
    ├── skip DOIs already in FLoRA (entry sheet + flora.csv) unless --no-skip-flora-validated
    ├── skip false_positive rows (Stage 2 already rejected them)
    ├── --resume: write every already-resolved row out first, re-run only target_pending
    ├── --extracted-test: write to extracted-test.csv, skip DOIs resolved in extracted.csv
    │
    └── for each remaining row:
            │
            ├── FRONT DOOR — classify_replication(doi_r, title_r, abstract_r)
            │       two models vote on the v3.2 schema; screen_gate() decides
            │       ├── gate says "discard"                → not_a_replication, row done
            │       ├── one vote answered                  → target_pending, row done
            │       ├── no votes answered                  → api_error, row done
            │       └── gate says "proceed"                → continue, carrying the
            │           screen's record_type and screen_categories into the row
            │
            └── _resolve_and_code():
                    run_for_doi(doi_r, classification=screen)   ← the ladder, below
                    │
                    ├── the ladder named targets but accepted none as THE link
                    │       → _per_target_rows(): one row per original PAPER
                    │         (collapse → guard → --resolved-only → outcome, once
                    │         per original, ranks renumbered 1..n after the drops)
                    │       → target_pending if no target could be matched
                    │
                    └── the ladder resolved one link
                            _merge_row()  → build the output row
                            _guard_original_link()  → reject self-links, recover doi_o
                            --resolved-only         → drop the row if it has no link
                            _outcome_without_coding() → outcome gate (below)
                                └── if codeable: parse cache → extract_outcome()
```

Nothing predicts how many originals a paper targets: the merged target prompt answers
it, and `original_match_type` records what came back (`multiple_original` when the
adapter wrote more than one row, `single_original` otherwise).

A row that the front door ends never pays for the ladder, PDF acquisition or outcome
coding. `--rescreen` reopens exactly the rows a previous run set
aside on the screen's own verdict, wherever a previous run's rows are being read
(`--resume`, or the production CSV that `--extracted-test` skips against), so a changed
voter pair or prompt decides them again. Every other resolved row is carried forward
untouched, and a multi-original paper is reopened as a unit.

## The front door (`shared/llm_client.classify_replication`)

Two providers vote on the validated **v3.2** schema — `classification` ∈
{`replication`, `reproduction`, `both`, `none`, `unclear`}, boolean `confident`, an
array of `categories` from an 11-value enum, `evidence_quote`, `reasoning`:

| Voter | Provider | Model |
| ----- | -------- | ----- |
| 1 | Gemini | `GEMINI_LIGHT_MODEL` |
| 2 | OpenAI, or OpenRouter when the id contains `/` | `SCREEN_VOTER2_MODEL` (default `gpt-5.4-mini`) |

`run_extract` refuses to start without `GEMINI_API_KEY` and whichever of
`OPENAI_API_KEY` / `OPENROUTER_API_KEY` voter 2 needs (unless `--no-llm`), because with
one provider every row returns a single vote, which is not a verdict. Voter 2 sits
outside the Google lineage on purpose: its errors overlap little with voter 1's.

**The gate is `screen_gate()`** — G-softqual from the v3.2 sweep, defined once and
called from both the front door and the batch-tools path in `link_original.py`. It
discards when every vote is `none` at any confidence, or when one voter said `none`
confidently and every other vote is a qualifying-or-`unclear` answer with
`confident: false`. Everything else proceeds: a confident `none` against a confident
qualifying answer is a real split, and it goes down the ladder rather than terminating.
There is no `screen_disagreement` outcome any more.

The screen also sets `record_type` (both voters agreeing on a qualifying label wins; a
`both` answer or a split falls back to voter 1's, and `both` maps to `replication`) and
`screen_categories` (the union of both voters' categories, `|`-joined in enum order).

A screen that did not get both votes is an API failure, not a verdict: it is returned
uncached (`llm_refscreen_partial` with one vote, `llm_refscreen_failed` with none) so a
re-run can decide the row once the provider is back. Discarded rows carry each voter's
label and confidence in `link_evidence` and both model names in `link_llm_model` — the
verdict alone is not something a reviewer can act on.

Cached at `cache/llm/classify_{key}.json`, keyed on the prompt version, **both** model
names and the abstract itself.

## The may-not-short-circuit gate (`link_original.may_stop_at_a_rule`)

The ladder below returns at the first rung that resolves, so a paper with one
conspicuous target can end at a rule before anything enumerates the rest. No rule
asserts that a paper has several originals any more; a rule may only **withhold** the
cheap path:

```
may_stop_at_a_rule(title_r, abstract_r, year_r)
    exactly one distinct author-year pair across title + abstract,
    AND no stated study count ("replications of N studies", 3 ≤ N < 1900)
        → a deterministic rung may END the row
    otherwise
        → its pick is WITHHELD until something that can enumerate targets speaks
```

The pick is withheld, never discarded. It is restored:

- at every exit where nothing enumerated — `--no-pdf`, no document acquired, no
  context, an incomplete screen (`--no-llm` never withholds, since nothing there could
  ever enumerate and the ladder would only pay for a PDF);
- after the full-text call, when that call **answered** and either named no target or
  named the same work (compared on the mapped record's DOI, falling back to
  title + year + first author).

A provider failure is not an answer: it exits as `llm_failed` carrying its `llm_error`,
so a re-run asks again rather than freezing an unconfirmed rule pick into a resolved
row. A false positive on the gate costs one LLM call; a false negative silently drops
N-1 originals. Project names are not a signal: Many Labs is many labs replicating
**one** original, and a Registered Replication Report likewise.

A later rung can also settle on ONE original for a paper an earlier successful call
already saw two in — its reference list was simply shorter. The ladder keeps the answer
with the most `match_certain` targets, and emits the union rather than the single link,
so the adapter writes every original including the one that rung resolved.

## Original-study resolution ladder (`link_original.run_for_doi`)

Cheapest first; the function returns at the first rung that resolves, so the PDF is a
last resort rather than the normal path. The front door's verdict is threaded in as the
`classification` argument, so the target pick reuses it instead of voting again (a
caller with no verdict — the batch tools — lets the screen vote there).

| # | Rung | Fires when | `link_method` on success |
| - | ---- | ---------- | ------------------------ |
| 1 | Base data | always — FLoRA sheet row + candidate pass-through fields | (not a resolver) |
| 2 | OpenAlex candidate re-query | always — builds the candidate pool from `referenced_works` | (not a resolver) |
| 2.5 | Title-pattern resolver | the title matches "A Replication of X" and one candidate matches that target by Jaccard (≥ 0.4, and ≥ 1.5× the runner-up) | `title_pattern_match` |
| 3 | Rule-based resolver | the abstract carries an author-year citation matching a candidate, or exactly one candidate came back | `citation_context_match`, `same_author_year_title_overlap`, `single_candidate_after_requery` |
| 4 | Abstract LLM | the abstract carries author-year patterns and there are candidates to choose from | `llm_cited_candidates` |
| 4.5 | Reference-list target pick | there are referenced works (OpenAlex, or OpenCitations as fallback) | `llm_references` |
| 4.6 | Pre-PDF title search | the screen agreed at high confidence that this is a replication and named a target it could not match to any reference | `llm_title_search` (**provisional**) |
| 5 | PDF acquisition + full-text LLM | everything above declined | `llm_fulltext`, `llm_title_search` (**provisional**) |

Rungs 2.5–4 all depend on `find_all_candidates()`, which returns `[]` unless the title
or abstract contains a parseable `(Author, Year)` citation. Many abstracts — clinical
and life-sciences ones especially — carry none, so for those papers the ladder starts
at 4.5.

**Rungs 4, 4.5 and 7 are one prompt.** `build_target_prompt()` asks the same question
at all three — which previously published study or studies does this paper re-test —
and only the evidence blocks differ: the abstract stage sends the abstract and the
candidates, 4.5 adds the reference list, the full-text stage adds the PDF abstract's
tail, the introduction and the methods. The three prompts it replaces asked three
different questions ("pick a candidate number", "pick a reference number", "how many
originals?"), so one rung could resolve a single original for a paper another had just
read as targeting twenty-eight.

Candidates and references are shown as ONE deduplicated `@smith2009` namespace
(`assign_target_keys()` in `shared/target_keys.py`): a work in both lists is offered
once, and a returned key is only ever resolved against the key_map from the same call.
`identify_targets_with_llm()` (`shared/llm_client.py`) makes the call and trusts
nothing the model says about a key — an invented key is demoted to an unmatched target,
a repeated key keeps its first entry, and `doi_o` comes from the mapped record rather
than from the model. `stated_count` / `unidentified_count` are reported so a shortfall
against a paper's own claimed count lands in `link_evidence` instead of vanishing.

A target is accepted **only when the model marks it `match_certain`**; otherwise the
row escalates, because a wrong original is worse than a slow one, and the prompt says
explicitly that most abstracts do not name their target and that declining is the
expected answer. When the call returns two or more targets, no single link is written: the whole target
list — mapped records included — reaches `_resolve_and_code()`, which hands it to
`_per_target_rows()` for one row per original. A target the model saw but could not
match to a record gets no row (there is no published record to write one about); the
shortfall is reported in the `link_evidence` of every row that was written, and a paper
where nothing matched is written `target_pending`.

The 4.5 pick is cached separately from the classification
(`cache/llm/reftarget_{key}.json`) — the two halves are decided at different points in
the pipeline. Each record's DOI/OpenAlex id is folded into the cache key on its own
account, because the prompt never shows the DOI: two lists that render identically can
still map the same key to a different record, and replaying the first answer would
write the stale original.

**Rungs 4.6 and 5** can both resolve a DOI by searching CrossRef/OpenAlex for a title
the model named, and both record `llm_title_search`. This is the one link method whose
answer is not chosen from a bounded candidate set — a paper that is not a replication
can be confidently linked to a landmark it merely cites — and a hand-check measured it
near 50% precision. It is therefore **not** in `RESOLVED_LINK_METHODS`:
`link_confidence` is forced to `low`, no outcome is coded, `csv_to_db` does not import
the row, and `sanity_check` moves it to `data/provisional_title_search.csv` for human
confirmation. Rung 4.6 is additionally gated on both voters having called the paper a
replication at high confidence.

If no PDF and no OpenAlex XML can be acquired, the row is written `target_pending`
rather than sent to the LLM with nothing to read — that is exactly how a confident,
fabricated `doi_o` gets produced.

### Guarding the link

`_guard_original_link()` runs on every produced row before it is written:

- a paper is never its own original (same DOI or identical title) → `target_pending`
- `doi_o` blank but `title_o` present → try CrossRef then OpenAlex title search
- still no DOI but the title is substantive and distinct → keep the row with
  `doi_o_verification = "no_doi"` (old papers, book chapters and working papers have
  genuine originals with no registered DOI)
- no DOI and no usable title → `target_pending`

`_verify_row()` then checks that `doi_o` actually points at the claimed paper (the
thresholds and the three correction tiers are in `shared/doi_verify.py`; see also
*DOI Verification* in `CLAUDE.md`), and `_fill_work_ids()` stamps
`oa_work_id_r` / `oa_work_id_o` afterwards, so the o-side ID always describes the DOI
finally written.

## Outcome coding

`_outcome_without_coding()` in `run_extract.py` is the single gate. A row whose
`link_method` is not in `RESOLVED_LINK_METHODS` has no confirmed original to code an
outcome against, so no outcome LLM runs:

| `link_method` | `outcome` written |
| ------------- | ----------------- |
| in `RESOLVED_LINK_METHODS` | coded by `extract_outcome()` |
| `not_a_replication` | `not_a_replication` — the screen's verdict *is* the outcome |
| `api_error` | `api_error` |
| `llm_title_search` | `cannot_be_determined` (provisional link, outcome deferred) |
| `target_pending`, `no_original_found`, and the historical `screen_disagreement` | `pending`, with the reason in `outcome_reasoning` |

The order per row is resolve → merge → guard → `--resolved-only` → outcome, so a row
the guard demotes or `--resolved-only` discards never reaches the outcome call.

```
extract_outcome(doi_r, abstract_r, fulltext, title_r, record_type=…)
    │
    ├── record_type == "reproduction" → straight to the LLM on the two
    │   computation/robustness axes (the replication keyword patterns would code it
    │   in the wrong vocabulary)
    │
    ├── --no-llm → keyword scan of the title (high-confidence hits only) then the
    │   abstract; fulltext is never keyword-scanned, because an introduction's
    │   background prose about OTHER studies' outcomes misfires the patterns
    │
    └── otherwise → _llm_outcome():
            abstract pass (GEMINI_HEAVY_MODEL, OpenAI preferred on retry)
            └── leaves the verdict unsettled (a reproduction: EITHER axis at
                cannot_be_determined), or there is no abstract, and parsed fulltext
                exists → second call over the paper's DISCUSSION AND CONCLUSION
                (8,000-char cap), whose answer replaces the first entirely — never
                one axis from each; disable with OUTCOME_FULLTEXT_ESCALATION=false
            only that full-text call judges record_type_check: "neither" →
                not_a_replication; the other vocabulary → the row is re-coded once
                under the other prompt and `type` is corrected (one hop, no loop)
```

The escalation text is chosen by `pdf_parsing.outcome_text()`, which slices from the
last discussion/conclusion heading to the reference list, and falls back to the closing
pages before the references when no heading is found. That is FLoRA's rule — "what
replication authors say in the abstract, or if not stated there, what is written in the
report (discussion and conclusion sections)" — and it fixes a real misread: the
escalation used to send the first 8,000 characters of the parse, which for a typical
article is the introduction and methods. The introduction is the one section that
routinely reports OTHER studies' replication failures. The text is labelled with a
`SOURCE:` line so the model never attributes a quote to a section it was not shown.

Exhausting every provider yields `outcome = api_error`, never a verdict, and is not
cached — `cannot_be_determined` is a judgement about the paper, and recording one for a
quota outage would make the two indistinguishable.

Every "is this a genuine replication?" decision is seen by the LLM when one is
available: a bare keyword hit like "failed to replicate" can fire on background prose,
so short-circuiting on it would let non-replications through as coded replications.

The text handed to the outcome LLM is the best-scoring entry in the parse cache
(`_best_fulltext_from_cache`), preferring `raw_text` and falling back to
`abstract + intro`. An all-empty parse cache counts as a miss — it is what a PDF-less
run wrote, and reading it back would pin the paper to abstract-only coding forever.

## PDF parse scoring

```
score = refs × 300 + abstract_len + intro_len × 2 + min(raw_text_len ÷ 5, 1000)
```

`parse_all()` runs six methods and `best_parse_result()` picks the winner by that
formula — the same one behind the ★ USED BY LLM badge in the app. The winner's text
goes to the DOI-resolution LLM, and its references become the structured reference
list in the prompt.

## Caching

Every LLM call is keyed with `content_key()`, which names everything the answer depends
on: the prompt's version (`prompt_version()`, the hash of the prompt text and every
fragment it splices in), the model(s) that will answer, and the inputs actually sent.
Editing a prompt therefore invalidates exactly the caches that depended on the old
wording, with nothing to bump by hand.

| Cache | Contents |
| ----- | -------- |
| `cache/llm/classify_*.json` | front-door verdicts (complete screens only) |
| `cache/llm/reftarget_*.json` | reference-list target picks |
| `cache/llm/llm_*.json` | abstract-level and full-text identification |
| `cache/llm/outcome_*.json` | outcome verdicts, including escalations |
| `cache/parse/parse_*.json` | per-method parse results |

Declines are cached; API failures are not.

## Post-run quarantine (`extract/sanity_check.py`)

Runs at the end of every `run_extract` and standalone. Rows that do not belong in the
resolved set are moved out to set-aside CSVs, **first match wins**, in this order:

| Bucket | Destination | Rule |
| ------ | ----------- | ---- |
| `screen_disagreement` | `screen_disagreement.csv` | `link_method == screen_disagreement` — **historical rows only**; the front door no longer emits it |
| `not_a_replication` | `not_a_replication.csv` | `outcome == not_a_replication` |
| `non_article` | `not_a_replication.csv` | `doi_r` is a figshare data record / peer-review object |
| `self_link` | `unresolved_self_links.csv` | `doi_o == doi_r` |
| `doi_mismatch` | `unresolved_doi_mismatch.csv` | `doi_o_verification == mismatch` |
| `title_search_provisional` | `provisional_title_search.csv` | `link_method == llm_title_search` |
| `target_pending` | `target_pending.csv` | `link_method == target_pending` |
| `fabricated_doi_o` | `fabricated_original_doi.csv` | `--deep` only: `doi_o` present but doi.org 404s |

Order is load-bearing. Old `screen_disagreement` rows are claimed first so that one
whose outcome happened to be coded `not_a_replication` cannot land in the agreed-no file
and bias any precision computed over it.

`cannot_be_determined` rows stay in `extracted.csv` — a linked original with an
undecidable outcome is still a real record. Chronology errors, duplicate `pair_id`s and
blank `doi_r` are reported but never moved; the right fix depends on diagnosis.

## Test sandbox

With `--extracted-test`, output goes to `data/extracted-test.csv` and DOIs already
resolved in `extracted.csv` are skipped, so a test run does not re-process production
rows. `--doi-r` targets are always processed, sandbox or not.

```bash
python -m extract.promote_test --all           # promote all
python -m extract.promote_test --doi 10.xxx/y  # promote one DOI
python -m extract.promote_test --all --dry-run # preview
```

## Key functions

| Function | File | Description |
|----------|------|-------------|
| `run_extract()` | `extract/run_extract.py` | Main orchestrator |
| `_front_door_row()` | `extract/run_extract.py` | Turns a screen verdict into a finished row, or `None` to continue |
| `may_stop_at_a_rule()` | `extract/link_original.py` | Whether a deterministic rung may end the row |
| `_resolve_and_code()` | `extract/run_extract.py` | Ladder → merge → guard → outcome for one row |
| `_per_target_rows()` | `extract/run_extract.py` | One row per original the target prompt named |
| `_outcome_without_coding()` | `extract/run_extract.py` | The outcome gate |
| `_guard_original_link()` | `extract/run_extract.py` | Self-link rejection and `doi_o` recovery |
| `classify_replication()` | `shared/llm_client.py` | Two-model front-door vote |
| `screen_references_with_llm()` | `shared/llm_client.py` | Rung 4.5: threads the verdict in, delegates the pick |
| `run_for_doi()` | `extract/link_original.py` | The resolution ladder |
| `identify_targets_with_llm()` | `shared/llm_client.py` | The merged target prompt: rungs 4, 4.5 and 7 |
| `assign_target_keys()` | `shared/target_keys.py` | One `@key` namespace over candidates + references |
| `extract_outcome()` | `extract/code_outcome.py` | Outcome coding |
| `find_all_candidates()` | `shared/openalex_client.py` | Candidate search |
| `parse_all()` / `best_parse_result()` | `shared/pdf_parsing.py` | Run all PDF parsers, score and pick |
| `verify_and_correct()` | `shared/doi_verify.py` | `doi_o` verification |
| `run_sanity_check()` | `extract/sanity_check.py` | Post-run quarantine |
