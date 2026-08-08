# Stage 3: Extract — Code Flow

**Entry points:** `python -m extract.tier --run` (decide and record), then
`python -m extract.export --release <id>` (render `data/extracted.csv`).

## What it does

For each work Stage 2's screen admitted, Stage 3 answers two questions:

1. Which original study does this paper target (`doi_o`, `title_o`, `link_method`)?
2. What was the result (`outcome`, `outcome_phrase`)?

The answer is stored as a permanent RESULT verdict row whose payload rebuilds that
work's `EXTRACTED_COLS` rows offline, with no network, cache, store or pool
(`extract/tier.py`). `EXTRACT_WORKERS` (default 4) works are in flight at once — a work
is minutes of provider and download latency and works are independent — and the
per-service reservation queue in `shared/rate_limit.py` bounds the request rate. Every
row passes DOI verification and `oa_work_id_*` stamping inside the judge, once, so the
answer is stored rather than recomputed per render.

`python -m extract.export --release <id>` then renders the stored payloads into
`data/extracted.csv`, whole and sorted, partitioning set-aside rows on the way out.
Only the works that release admits are rendered: a verdict outlives the routing that
bought it, and `--all-releases` is the explicit ask for every stored verdict. It is that file's only
writer; `extract/sanity_check.py` reports on it and moves nothing.

The whole stage is organised around spending the cheap calls first. The classification
screen discards before anything is spent on a row — 89% of the adjudicated hard
negatives in the v3.2 gate sweep, with zero settled misses; there is no measured
discard rate over a production pile, so do not quote one. The resolution ladder returns at the
first rung that resolves, and the outcome LLM — the most expensive call, because it can
escalate to 8,000 characters of full text — runs only on a row that already has a
confirmed original.

## Per-row flow

```
extract/tier.py — extract_works(): the worklist
    │
    ├── every work a live current-generation screen_expensive PROCEED verdict admits,
    │       its row built in process by iter_export_rows + screen_columns
    ├── minus works this tier has already SETTLED (target_pending and api_error do not settle)
    ├── minus works under another runner's unexpired extract claim
    ├── minus DOIs already in FLoRA (entry sheet + flora.csv)
    ├── minus works already in the validation tables (data/validated_skip.csv)
    │
    └── claim EXTRACT_CLAIM_BATCH works, then for each of them — _judge():
            │
            ├── FRONT DOOR — _screen_from_row(row): the verdict Stage 2 wrote
            │       into SCREEN_COLS, rebuilt as the classify_replication() dict.
            │       No call is made here — classify_replication is not imported
            │       ├── screen_verdict "discard"           → not_a_replication, row done
            │       ├── screen_verdict blank               → target_pending, row done
            │       └── screen_verdict "proceed"           → continue, carrying the
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
coding — but it is still a RESULT verdict, so the work is settled and is not offered
again. Reopening one takes `--redo`, or a new extract generation (a changed prompt,
model or ladder version), which reopens every work at once.

## The cheap discard-only tier (`shared/prescreen.py` + `filter/engine/tiers.py`)

Not part of Stage 3, and currently DORMANT — all three `screen_cheap` specs are
`shadow: true`, so nothing is routed to the pile (waking it: `docs/filter-engine.md`,
"Activating the cheap tier"). Which rows would get it is a Stage 2 routing decision —
the rule book sends a row to the `screen_cheap` pile and `filter/engine/tiers.py` runs
the tier over that pile — so a row arriving at the front door has already been routed
past it. It is described here because its verdicts decide Stage 3's rows. Two
very small models (`PRESCREEN_MODEL_1`, `PRESCREEN_MODEL_2`, OpenRouter by
default) answer one question with one field; voter 2 is asked only when voter 1 said
"no", because once the row can no longer be discarded a second opinion changes nothing.

The tier may only **discard**, and only on two explicit noes. One keep, an unrecognised
label, an unreadable reply or a provider failure all pass the row through to the front
door unchanged, and non-answers are never cached. Three classes of row are never
pre-screened at all: text stating the design outright (`hard_signal()`), rows from a
`CURATED_SOURCES` list, and rows with under `PRESCREEN_MIN_ABSTRACT_CHARS` of abstract.

A live discard drops the row at the handoff, so Stage 3 never sees it and writes
nothing. `link_method = prescreen_discard` therefore has no live writer: rows on disk
from when the tier ran inside Stage 3 still carry it, `sanity_check` still files them
in `data/prescreen_discard.csv`. A cheap verdict
never ADMITS a row either — its `proceed` means "on to the expensive screen", so it
leaves the work unscreened for the screened-only handoff. Evidence on the tier's
precision: `analysis/prescreen_eval/REPORT.md` and
[limitations.md](../limitations.md) §(g).

## The front door (`shared/llm_client.classify_replication`), run by Stage 2

The call lives in Stage 2's `screen_expensive` tier. Stage 3 reads its answer off
the input row (`SCREEN_COLS`: `screen_verdict`, `screen_record_type`,
`screen_categories`, `screen_votes`, `screen_evidence`, `screen_reasoning`) and
rebuilds this dict with `_screen_from_row()`. It never runs the call: a row whose
verdict is missing is written `target_pending`, and the tier's worklist does not offer
a work no live current-generation verdict admitted. What the two voters do, below, is
unchanged — only where.

Two providers vote on the validated schema, at prompt version **v3.3** (v3.2 plus the
partial-overlap rule; evaluated copy `analysis/screening_eval/prompt_v33.txt`, evidence
`analysis/screening_eval/report_v33.md`) — `classification` ∈
{`replication`, `reproduction`, `both`, `none`, `unclear`}, boolean `confident`, an
array of `categories` from an 11-value enum, `evidence_quote`, `reasoning`:

| Voter | Provider | Model |
| ----- | -------- | ----- |
| 1 | Gemini | `SCREENING_MODEL_1` |
| 2 | OpenAI, or OpenRouter when the id contains `/` | `SCREENING_MODEL_2` (default `gpt-5.4-mini`) |

Which key each voter needs follows its model id through `provider_for()`, not a
hardcoded pair: with today's defaults it is `GEMINI_API_KEY` for voter 1 and whichever
of `OPENAI_API_KEY` / `OPENROUTER_API_KEY` voter 2 needs. A Stage 3 run calls neither
voter and needs neither key. Voter 2 sits
outside the Google lineage on purpose: its errors overlap little with voter 1's.

**The gate is `screen_gate()`** — G-softqual from the v3.2 sweep, defined once and
called from both the front door and the batch-tools path in `link_original.py`. It
discards when every vote is `none` at any confidence, or when one voter said `none`
confidently and every other vote is a qualifying-or-`unclear` answer with
`confident: false`. Everything else proceeds: a confident `none` against a confident
qualifying answer is a real split, and it goes down the ladder rather than terminating.
There is no `screen_disagreement` outcome any more.

The screen also sets `record_type` (both voters agreeing on a qualifying label wins; a
`both` answer or a split falls back to the FIRST QUALIFYING voter's — not voter 1's,
since a non-qualifying voter 1 is skipped — and `both` maps to `replication`) and
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

**Two of the three rule methods are held whatever the gate says.**
`_HELD_ONLY_METHODS` in `link_original.py` — `single_candidate_after_requery` and
`same_author_year_title_overlap` — may never END the ladder, even for a paper whose
own text names exactly one target. Neither carries a semantic check: the lone-candidate
branch accepts whatever survived the re-query, and the title-overlap branch breaks on a
≥ 0.05 token overlap a tie that Path A's citation score refused to break. Their picks
are held until something that can enumerate targets confirms or contradicts them — the
abstract LLM when the abstract carries an author-year pattern, the reference-list pick
otherwise — and restored at the exits listed above when nothing enumerating ever spoke.
`citation_context_match` and `title_pattern_match` are the two that can still end a row
outright, and only when the gate lets them.

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
| 3 | Rule-based resolver | the abstract carries an author-year citation matching a candidate, or exactly one candidate came back | `citation_context_match`; `same_author_year_title_overlap` and `single_candidate_after_requery` **held only** — never terminal, see above |
| 4 | Abstract LLM | the abstract carries author-year patterns and there are candidates to choose from | `llm_cited_candidates` |
| 4.5 | Reference-list target pick | there are referenced works (OpenAlex, or OpenCitations as fallback) | `llm_references` |
| 4.6 | Pre-PDF title search | the screen agreed at high confidence that this is a replication and named a target it could not match to any reference | `llm_title_search` (**provisional**) |
| 5 | PDF acquisition + full-text LLM | everything above declined | `llm_fulltext`, `llm_title_search` (**provisional**) |

Rungs 2.5–4 all depend on `find_all_candidates()`, which returns `[]` unless the title
or abstract contains a parseable `(Author, Year)` citation. Many abstracts — clinical
and life-sciences ones especially — carry none, so for those papers the ladder starts
at 4.5.

**Rungs 4, 4.5 and 5 are one prompt, and it asks both questions.**
`build_target_outcome_prompt()` (and its reproduction twin) asks the same thing at all
three — which previously published study or studies does this paper re-test, and what
did it conclude about each — and only the evidence blocks differ: the abstract stage
sends the abstract and the candidates, 4.5 adds the reference list, the full-text stage
adds the PDF abstract's tail, the introduction, the methods and the DISCUSSION /
CONCLUSION slice. The three prompts it replaces asked three different questions ("pick
a candidate number", "pick a reference number", "how many originals?"), so one rung
could resolve a single original for a paper another had just read as targeting
twenty-eight.

Coding the outcome here is what makes it answerable at all. Asked separately it was
handed the target as an asserted header it could not check, and its full-text
escalation could never fire — a row resolved from the abstract never acquired a
document, so 5 of 285 rows in `data/extracted.csv` carry any `pdf_source` and every
stored `out_quote_source` is a title or an abstract. A rung now ends the row only when
it resolved AND settled the outcome; an unsettled verdict carries the resolution down
the ladder (`OUTCOME_DESCENT`).

Candidates and references are shown as ONE deduplicated `@smith2009` namespace
(`assign_target_keys()` in `shared/target_keys.py`): a work in both lists is offered
once, and a returned key is only ever resolved against the key_map from the same call.
`resolve_targets_and_outcomes()` (`shared/llm_client.py`) makes the call and trusts
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
`link_confidence` is forced to `low`, no outcome is coded, the validation import does
not take the row, and `sanity_check` moves it to `data/provisional_title_search.csv` for human
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

`_outcome_without_coding()` in `extract/run_extract.py` is the single gate. A row whose
`link_method` is not in `RESOLVED_LINK_METHODS` has no confirmed original to code an
outcome against, so no outcome LLM runs:

| `link_method` | `outcome` written |
| ------------- | ----------------- |
| in `RESOLVED_LINK_METHODS` | coded by `extract_outcome()` |
| `not_a_replication` | `not_a_replication` — the screen's verdict *is* the outcome |
| `prescreen_discard` | `not_a_replication` at `low` confidence — the cheap tier's verdict, kept out of the validated screen's file |
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
    └── the target call already coded it → that block is the outcome, no second call
    │
    └── otherwise (a deterministic rule resolved the link) → _llm_outcome():
            ONE call over every passage the row has: the abstract, and — when a
                document was acquired — INTRODUCTION and DISCUSSION / CONCLUSION,
                each named, the latter with its provenance
            the named original is evidence to CHECK, with the link evidence that
                produced it: target_check answers whether the text bears it out
            with any text sent, record_type_check and target_check are asked:
                "neither" / "no_original" → not_a_replication; "other_original" →
                link_confidence low, noted in link_evidence; the other vocabulary →
                the row is re-coded once under the other prompt and `type` is
                corrected (one hop, no loop)
```

The closing text is chosen by `pdf_parsing.outcome_text()`, which slices from the
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
| `cache/llm/classify_*.json` | front-door verdicts (complete screens only). Written by Stage 2's expensive tier; never read in Stage 3, and read by the handoff through `cached_classification()` for the category union and the voters' reasoning |
| `cache/llm/reftarget_*.json` | reference-list target picks |
| `cache/llm/llm_*.json` | abstract-level and full-text identification |
| `cache/llm/outcome_*.json` | outcome verdicts, including escalations |
| `cache/parse/parse_*.json` | per-method parse results — read by the ladder before it parses, and by `_best_fulltext_from_cache` |
| `cache/pdfs/pdfsrc_*.json` | per-DOI `{source, url}` — which tier really supplied the saved PDF, replayed by `acquire_pdf`'s up-front on-disk check |
| `cache/pdfs/retry_*.json` | per-DOI `{tier: timestamp}` — which acquisition tiers came back empty and when. The same file shape keyed on `url:<URL>` holds back one URL the server answered 404/410 for |
| `cache/openalex_xml/retry_*.json` | per-work timestamp of the last content-free GROBID-XML fetch |

Declines are cached; API failures are not.

**Acquisition retry delays.** `data/target_pending.csv` is reopened by every run and
almost none of its rows have a document, so each run used to re-pay the whole
eleven-tier waterfall: uncached failed downloads, landing-page scrapes, a headless
Chromium launch per row, and the metered OpenAlex content request. A tier that comes
back empty is now timestamped and not re-probed for `PDF_RETRY_AFTER_DAYS` (14, in
`shared/pdf_sources.py`); the content-free XML answer gets the same delay
(`OA_XML_RETRY_AFTER_DAYS`). This is a retry delay, never a verdict — the tier is asked
again once it lapses, and a tier skipped for a missing API key or a missing package is
not recorded at all, so a key added tomorrow takes effect tomorrow. A successful
acquisition clears the record. A single URL the server answered **404 or 410** for
gets its own record on the same window, which holds back that URL without holding
back the other URLs its tier offers; nothing else qualifies, because a timeout, a
429, a 5xx and a 401/403 are all the server failing to say "there is no document
here". And a row whose PDF is already on disk skips the waterfall outright,
reporting the tier recorded beside the file rather than whichever tier re-derived a
URL first.

**Tier 0 short-circuits the rest.** When the OpenAlex GROBID XML comes back with
content, that IS the document — the parsers read it exactly as they read a downloaded
PDF — so tiers 1–10 are skipped. Such a row has `pdf_source = openalex_xml`, no
`pdf_path` and `pdf_ok = false`, which is what it already had whenever the downloads
failed.

## The set-aside partition (`extract/export.py` + `extract/sanity_check.py`)

Rows that do not belong in the resolved set are written to a set-aside CSV instead of
`extracted.csv` — by the EXPORT, as it writes them, through `classify_row()` in
`sanity_check`. `python -m extract.sanity_check` applies the same rules to the exported
file and reports; it moves nothing. **First match wins**, in this order:

| Bucket | Destination | Rule |
| ------ | ----------- | ---- |
| `screen_disagreement` | `screen_disagreement.csv` | `link_method == screen_disagreement` — **historical rows only**; the front door no longer emits it |
| `non_article` | `not_a_replication.csv` | `doi_r` is a figshare data record / peer-review object |
| `title_search_provisional` | `provisional_title_search.csv` | `link_method == llm_title_search` |
| `target_pending` | `target_pending.csv` | `link_method == target_pending` |
| `prescreen_discard` | `prescreen_discard.csv` | `link_method == prescreen_discard` — the cheap pre-screen's own discards, kept out of `not_a_replication.csv` |
| `not_a_replication` | `not_a_replication.csv` | `outcome == not_a_replication` |
| `api_error` | `api_error.csv` | `link_method == api_error` — a transient provider failure; the next run retries it |
| `no_original_found` | `no_original_found.csv` | `link_method == no_original_found` — a settled LLM verdict a re-run would only pay to reproduce |
| `self_link` | `unresolved_self_links.csv` | `doi_o == doi_r` |
| `doi_mismatch` | `unresolved_doi_mismatch.csv` | `doi_o_verification == mismatch` |

**The set-asides belong to the CSV they came out of** (`set_aside_dir()` in
`shared/schema.py`): `extracted.csv`'s sit directly in `data/`, and a sandbox render
(`--out data/extracted-test.csv`) partitions into `data/extracted-test-set-aside/`.

Two further buckets exist only in the sanity REPORT, because each needs a network
lookup per row and so cannot be decided as a row is written: `non_article_type` (the
registry types `doi_r` as a non-study object) and `fabricated_doi_o` (`doi_o` present
but registered nowhere). Both name rows that are in `extracted.csv` and should not be;
`--deep` is what asks.

Order is load-bearing, and the principle is that **`link_method` rules come before the
`outcome` rule**: where a row sits in the pipeline is a fact about its identity, while
what its `outcome` column happens to say is a fact about a file's contents. So old
`screen_disagreement` rows are claimed first — one whose outcome was coded
`not_a_replication` must not land in the agreed-no file and bias any precision computed
over it — and `prescreen_discard` is claimed before the outcome rule for the same
reason: the pre-screen writes `outcome = not_a_replication` but is a weaker instrument
than the validated voter pair, and mixing its discards in would corrupt that file.

`cannot_be_determined` rows stay in `extracted.csv` — a linked original with an
undecidable outcome is still a real record. Chronology errors, duplicate `pair_id`s and
blank `doi_r` are reported and belong here; the right fix depends on diagnosis.

## Test sandbox

`--mode validation` records real verdicts against a real claim, and the mode lives in
`claim.meta.mode`, so the live export ignores them and they do not settle the live
worklist.

```bash
python -m extract.tier --run --mode validation --limit 20
python -m extract.export --release <id> --mode validation --out data/extracted-test.csv
```

There is no promotion step: re-running the work live is the promotion, and it is
near-free on cached calls.

## Key functions

| Function | File | Description |
|----------|------|-------------|
| `run_extract_tier()` | `extract/tier.py` | The run loop: claim a batch, judge it, rebuild the worklist |
| `_judge()` / `result_payload()` | `extract/tier.py` | One work through the pipeline, and the payload that stores its answer |
| `render()` / `partition()` | `extract/export.py` | Stored payloads → `data/extracted.csv` + the set-asides |
| `supersede_targets()` | `extract/tier.py` | The retroactive tools' write path: claim, correct, supersede |
| `_process_row()` | `extract/run_extract.py` | Every row the pipeline writes for one input row |
| `_front_door_row()` | `extract/run_extract.py` | Turns a screen verdict into a finished row, or `None` to continue |
| `may_stop_at_a_rule()` | `extract/link_original.py` | Whether a deterministic rung may end the row |
| `_resolve_and_code()` | `extract/run_extract.py` | Ladder → merge → guard → outcome for one row |
| `_per_target_rows()` | `extract/run_extract.py` | One row per original the target prompt named |
| `_outcome_without_coding()` | `extract/run_extract.py` | The outcome gate |
| `_guard_original_link()` | `extract/run_extract.py` | Self-link rejection and `doi_o` recovery |
| `classify_replication()` | `shared/llm_client.py` | Two-model front-door vote |
| `screen_references_with_llm()` | `shared/llm_client.py` | Rung 4.5: threads the verdict in, delegates the pick |
| `run_for_doi()` | `extract/link_original.py` | The resolution ladder |
| `resolve_targets_and_outcomes()` | `shared/llm_client.py` | The merged target+outcome prompt: rungs 4, 4.5 and 7 |
| `assign_target_keys()` | `shared/target_keys.py` | One `@key` namespace over candidates + references |
| `extract_outcome()` | `extract/code_outcome.py` | Outcome coding |
| `find_all_candidates()` | `shared/openalex_client.py` | Candidate search |
| `parse_all()` / `best_parse_result()` | `shared/pdf_parsing.py` | Run all PDF parsers, score and pick |
| `verify_and_correct()` | `shared/doi_verify.py` | `doi_o` verification |
| `classify_row()` | `extract/sanity_check.py` | Which set-aside a row belongs in |
| `run_sanity_check()` | `extract/sanity_check.py` | The integrity report over the exported CSV |
