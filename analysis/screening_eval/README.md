# Screening evaluation — derivation data for the Stage-3 voter pair

This directory holds the hand-coding and model-evaluation evidence behind Stage 3's
front-door screen (`classify_replication()` in `shared/llm_client.py`): the two-model
voter pair, the prompt wording, and the discard rule.

**These cases are derivation data.** Every case here was used while choosing the prompt
and the voter pair, so a number computed on them is an in-sample number. Any future
evaluation of a screening prompt or voter pair must hold these cases out or
cross-validate — see the "Evaluation design" section of
[`screening_prompt_proposal.md`](screening_prompt_proposal.md), which states the same
requirement and proposes FLoRA entries as the out-of-sample positive set.

Files were assembled from untracked scratch files in the repo root; the originals are
still there. The directory is self-contained: every script here except
`build_adjudication.py` runs from a fresh clone with no inputs outside it.
`build_adjudication.py` additionally needs the gitignored `cache/llm/` for abstract text,
so only it cannot be re-run from a clone.

---

## Case sets and truth labels

| File | Contents |
| --- | --- |
| `flora_coding_75_results.csv` | The human coder's raw returns: 75 rows, `case`, `block`, `ref`, `task`, `doi_r`, `your_verdict`, `your_confidence`, `your_note`, plus `revised_verdict`, `revised_reason`, `recoded` (see "Two labellings" below). 73 rows carry a verdict (two rows of block D were left blank). Blocks: `A_calibration` (15), `B_discarded` (22), `C_disagreement` (23), `D_link_accuracy` (15). First-pass verdicts across coded rows: 57 `no`, 10 `yes`, 6 `unclear`. |
| `human_cases.json` | The 60 screening cases the coder saw for blocks A/B/C, as `{id, title, abstract, bucket}` with ids `HU01`–`HU60`. `bucket` repeats the block name. This is the input file the voter scripts prompt from. |
| `human_truth.json` | `{"truth": {HU01…HU60: yes|no|unclear}}` — the coder's verdicts for those 60 cases, keyed by case number (`HU11` = case 11). Verified identical to the `your_verdict` column of the CSV: 56 `no`, 3 `yes`, 1 `unclear`. **These are the first-pass labels, before the coding-rule discussion recorded in `screening_prompt_proposal.md` §1–2.** |
| `human_truth_revised.json` | Same shape, same 60 ids, carrying the `revised_verdict` labels settled in `screening_prompt_proposal.md` §1–2: 50 `no`, 10 `yes`. Generated from the CSV's `revised_verdict` column; seven ids differ from `human_truth.json` (HU11, HU37, HU50, HU52, HU55, HU56, HU57, all now `yes`). |
| `heldout_cases.json` | 30 cases (`H001`–`H030`) drawn from the same gemma-vs-flash-lite benchmark as the adjudication set but sharing no source file with it (verified: zero overlap on the `file` key). Each carries `title`, `abstract`, the cache `file`, and the three benchmark verdicts `_flash_lite`, `_gemma`, `_ref`. |
| `heldout_truth.json` | `{"truth": {H001…H030}}` — 24 `no`, 6 `yes`. Produced by the same three-judge panel procedure as `adjudicated.json`, from the `hojudge_b*_j*.json` vote files (the script that folded those votes into this file was not promoted, so this file cannot be regenerated here). |
| `coding_sheet_75.csv` | The sheet the human coder worked from: the same 75 rows plus `title`, `abstract`, `context` and `panel_said` (the adjudication panel's verdict, which block A checks). `score_human.py` joins the returns to this on `case`. |
| `judge_b1…b4_j1…j3.json` | The 12 raw judge files behind `adjudicated.json` — three independent judges per blinded batch, each `{id, verdict, confidence, reason}`. |
| `hojudge_b1…b2_j1…j3.json` | The 6 raw judge files for the held-out set, same shape. |
| `gemma_eval_google_gemma-4-31b-it.json` | The gemma-vs-flash-lite benchmark run the case sets were drawn from. Keyed by LLM-cache `file`, with verdict + confidence for `flash_lite`, `cand` (gemma) and `ref` (gpt-5-mini). It is the only source of confidences for the two live production models, so `pair_analysis.py` depends on it. |
| `prompt_v2.txt` | The variant screening prompt; `eval_second_voter.py <model> v2` uses it, and the `_v2` voter files were produced with it. |

### Block meanings

`A_calibration` cases are ones the LLM adjudication panel had already ruled on, shown to
the human coder to check the panel. `B_discarded` are rows the live screen threw away as
not-a-replication. `C_disagreement` are rows where the two live screen models split.
`D_link_accuracy` is a different question — whether an `llm_references` link picked the
right original — and is not part of the screening truth sets.

### The adjudication panel and its calibration caveat

| File | Contents |
| --- | --- |
| `adjudication_cases.json` | 47 cases (`C001`–`C047`) built by `build_adjudication.py`: buckets `A_flashlite_no_ref_yes`, `B_gemma_yes_flashlite_no`, `C_other_disagreement` (the cases where the benchmarked models actually differ) plus `D_control_all_no` (8) and `D_control_all_yes` (6) agreement controls that detect a judge who just says yes. Each case keeps the models' verdicts under `_flash_lite`, `_gemma`, `_ref`. |
| `adjudication_batch1…4.json` | The blinded copies handed to the judges — `{id, title, abstract}` only, shuffled and dealt into four batches of 12/12/12/11. No bucket, no model verdicts. |
| `adjudicated.json` | `{"truth": …, "ambiguous": []}` — the panel's majority verdict for all 47 cases (36 `no`, 10 `yes`, 1 `unclear`); every case reached a majority. Regenerated byte-identically by `score_adjudication.py` from the repo-root judge files. |

**Calibration caveat.** Block A of the human coding re-checked 15 of these panel verdicts.
The human coder agreed with the panel on 11 of 15 — the panel and the coder disagree on
4 of the 15 calibration cases (`score_human.py` prints them: C016, C022, C033, C042).
Panel-derived truth (`adjudicated.json`, and by construction `heldout_truth.json`) is
therefore an LLM proxy for human coding, not a substitute for it, and numbers computed
against it should be reported as such.

**Two labellings.** `screening_prompt_proposal.md` §2 re-codes seven of the hand-coded
cases under the rules settled after the first pass (§1). All seven flip to `yes`: case 11
(block A), case 37 (block B) and cases 50, 52, 55, 56, 57 (block C). Six of the seven were
`no`; case 56 was `unclear`. Rule 3 — only the *initial* validation of a newly proposed
instrument is excluded, re-validation of a published one counts — drives every change.

Both labellings are now readable from data:

| Artifact | Labelling |
| --- | --- |
| `flora_coding_75_results.csv` → `your_verdict` | first pass |
| `flora_coding_75_results.csv` → `revised_verdict` | settled rules (repeats `your_verdict` where nothing changed, so it is directly usable as a truth column) |
| `flora_coding_75_results.csv` → `revised_reason`, `recoded` | the driving rule, and a `TRUE`/`FALSE` flag on the seven changed rows |
| `human_truth.json` | first pass — 56 `no`, 3 `yes`, 1 `unclear` |
| `human_truth_revised.json` | settled rules — 50 `no`, 10 `yes`, 0 `unclear`; same `HU01`–`HU60` shape and ids |
| the `_human` voter files below | scored against the **first-pass** labels |

`score_human.py` takes `--revised` to score `revised_verdict`; without the flag its output
is unchanged. Verified by running it both ways:

| | first pass | revised |
| --- | --- | --- |
| block B wrongly discarded | 0/22 = 0% | **1/22 = 5%** (case 37, the Japanese ecSI-2.0 translation) |
| block C genuine replications in the set-aside pile | 1/23 = 4% | 6/23 = 26% |
| block A agreement with the panel | 11/15 = 73% | 10/15 = 67% (case 11 now also disagrees) |
| positives across A/B/C | 3/60 | 10/60 |

So the "21 of 22 hand-checked discards were genuine negatives" figure is the **revised**
labelling, and it reproduces: 21 `no` / 1 `yes`. Under the first-pass labelling block B is
22/22. Any number quoted from this directory must say which labelling it uses.

§2 also records six cases that were discussed and *confirmed* unchanged (1, 2, 5, 12, 23,
40); they are not marked `recoded`, since their verdict did not move.

---

## Per-voter result files

`voter_<model>_<variant>[_set].json` — one file per model run, produced by
`eval_second_voter.py`. The model id has `/` replaced by `_`
(`voter_mistralai_ministral-14b-2512_…` = `mistralai/ministral-14b-2512`).

- `_prod` — the production screening prompt (embedded verbatim in `eval_second_voter.py` as `PROD_PROMPT`).
- `_v2` — the variant prompt draft (`prompt_v2.txt`).
- no set suffix — scored against the adjudication panel (`adjudicated.json`, 47 cases, `C…` ids).
- `_ho` — the held-out set (`heldout_cases.json` / `heldout_truth.json`, 30 cases, `H…` ids).
- `_human` — the human-coded set (`human_cases.json` / `human_truth.json`, 60 cases, `HU…` ids).

Each row is `{id, bucket, truth, verdict, conf, error, raw}`, so a file records both what
the model said and the confidence the discard rule needs.

| File | Set | n |
| --- | --- | --- |
| `voter_google_gemini-3.6-flash_prod.json` | panel | 47 |
| `voter_gpt-5.4-mini_prod.json` | panel | 47 |
| `voter_gpt-5.4-nano_prod.json` | panel | 47 |
| `voter_mistralai_ministral-14b-2512_prod.json` | panel | 47 |
| `voter_qwen_qwen3-30b-a3b-instruct-2507_prod.json` | panel | 47 |
| `voter_google_gemini-3.5-flash-lite_prod_ho.json` | held-out | 30 |
| `voter_google_gemini-3.5-flash-lite_v2_ho.json` | held-out | 30 |
| `voter_mistralai_ministral-14b-2512_prod_ho.json` | held-out | 30 |
| `voter_mistralai_ministral-14b-2512_v2_ho.json` | held-out | 30 |
| `voter_google_gemini-3.5-flash-lite_prod_human.json` | human | 60 |
| `voter_google_gemini-3.6-flash_prod_human.json` | human | 60 |
| `voter_mistralai_ministral-14b-2512_prod_human.json` | human | 60 |
| `voter_openai_gpt-5-mini_prod_human.json` | human | 60 |
| `voter_openai_gpt-5.4-mini_prod_human.json` | human | 60 |
| `voter_gpt-5-mini_prod_human.json` | human | 60 |

`voter_gpt-5-mini_prod_human.json` is a **failed run**: all 60 rows have `verdict: null`
and an `HTTP 429 … no credits remaining` error. The usable gpt-5-mini run on the human set
is `voter_openai_gpt-5-mini_prod_human.json`. Note that the two files are the same model
reached by different routes (direct OpenAI id vs `openai/…` via OpenRouter), which is why
the names differ only by the prefix.

The live production models `gemini-3.5-flash-lite` and `gpt-5-mini` were not re-run on the
47-case panel set — their verdicts there come from the cached benchmark run
(`gemma_eval_google_gemma-4-31b-it.json`), which is why no
`voter_…_prod.json` exists for them.

---

## What the pair analysis actually shows

`pair_analysis.py` is the file that substantiates the voter-pair choice, because it scores
**pairs under the production discard rule** — discard only when both models say `no` *and*
the weaker of the two confidences is `high` — rather than models in isolation. Output
below is the script's own, run over the 47 adjudicated cases (36 panel-`no`, 10 panel-`yes`,
1 `unclear`), holding `gemini-3.5-flash-lite` fixed as voter 1 and varying voter 2 across
the seven other measured models.

```
  second voter (with flash-lite)    correct disc  WRONG disc  disagree
  gemma-4-31b-it                   23/36 =  64%        2      14/47 = 30%
  google/gemini-3.6-flash          32/36 =  89%        5      7/47 = 15%
  gpt-5-mini                       9/36 =  25%        0      31/47 = 66%
  gpt-5.4-mini                     23/36 =  64%        0      18/47 = 38%
  gpt-5.4-nano                     13/36 =  36%        0      26/47 = 55%
  mistralai/ministral-14b-2512     32/36 =  89%        0      8/47 = 17%
  qwen/qwen3-30b-a3b-instruct-2507 25/36 =  69%        2      11/47 = 23%
```

`correct disc` = true negatives the pair removes (throughput); `WRONG disc` = real
replications silently lost (the costly error); `disagree` = rows sent to
`screen_disagreement.csv`.

**This is where the "89% vs 25%" comparison comes from.** The production pair
(flash-lite + `mistralai/ministral-14b-2512`) correctly discards 32 of 36 true negatives
= 89% while losing **0** of the 10 real replications; pairing flash-lite with gpt-5-mini
instead discards only 9 of 36 = 25%, sending 66% of cases to the disagreement pile. The
one other pair that also reaches 89%, `google/gemini-3.6-flash`, buys that throughput
with 5 wrongly discarded replications, which is the error the design treats as
unacceptable — so ministral is the only candidate that is simultaneously at the top on
throughput and at zero on the costly error.

The same run's solo accuracies against the panel put ministral first
(42/47 = 89.4%, 1/10 missed replications, 3/36 false positives) ahead of flash-lite
(39/47 = 83.0%, 4/10, 3/36) and gpt-5-mini last (17/47 = 36.2%, 1/10, 18/36), and its
complementarity line reads "fixes 5 flash-lite errors, introduces 2 new ones" — the best
ratio of the seven.

Two caveats carry over: truth here is the LLM judge panel, which the human coder disagreed
with on 4 of 15 calibration cases, and the cases were selected for model disagreement plus
14 agreement controls, so these rates describe hard cases and are not a base rate for the
live queue.

---

## Scripts and how they consume the files

All five run from this directory (`python3 <script>.py`); each resolves its inputs via
`Path(__file__)`, so the working directory does not matter.

**`build_adjudication.py`** — builds `adjudication_cases.json` and the four blinded
`adjudication_batch*.json` files from `gemma_eval_google_gemma-4-31b-it.json` and the
abstracts cached under the repo's `cache/llm/`. That cache is gitignored, so this is the
one script that cannot be re-run from a fresh clone. Re-running it overwrites the case and
batch files (the sampling is seeded, `random.seed(20260729)`).

**`score_adjudication.py`** — reads `adjudication_cases.json` plus the 12 `judge_b*_j*.json`
vote files, takes the 2-of-3 majority per case as truth, reports flash-lite / gemma /
gpt-5-mini accuracy overall, by direction (missed replications vs false positives) and by
bucket, and writes `adjudicated.json`. Verified: all 47 cases have 3 votes, all reach a
majority, 43 unanimous, and the rewritten `adjudicated.json` is byte-identical to the one
checked in. It ignores `hojudge_*` — the held-out panel is not folded in by any promoted
script.

**`pair_analysis.py`** — the pair-level scorer described above. Reads `adjudicated.json`,
`adjudication_cases.json`, `gemma_eval_google_gemma-4-31b-it.json` (for the two production
models' cached verdicts *and confidences*) and every `voter_*_prod.json` in this directory,
then prints solo accuracy, pair behaviour under the discard rule, and complementarity
against flash-lite. Adding a `voter_<model>_prod.json` file adds that model to the tables.

**`eval_second_voter.py`** — the only script that spends money. `python3 eval_second_voter.py <model-id> [prod|v2]`, with
`EVALSET=human` or `EVALSET=heldout` selecting the case/truth pair (default: the
adjudication panel). Model ids containing `/` go via OpenRouter (needs
`OPENROUTER_API_KEY`), otherwise direct OpenAI (`OPENAI_API_KEY`). It prompts the model
once per case with the chosen prompt, writes a `voter_*.json` file here, and prints
accuracy, misses and false positives against the selected truth. The output suffix it
generates for a held-out run is `_heldout`; the checked-in files use `_ho`.

**`score_human.py`** — reads `flora_coding_75_results.csv`, joins it to the case texts and
panel verdicts in `coding_sheet_75.csv`, and reports: block A panel calibration
(11/15 = 73% agreement), block B wrongly-discarded rate (0/22 under the first-pass labels),
block C genuine replications in the set-aside pile (1/23), block D link accuracy
(7/13 correct originals, on the 13 of 15 rows the coder returned), and an extrapolation of
each rate to the full pool it was sampled from. `--revised` re-runs the same report against
the `revised_verdict` column (73% → 67%, 0/22 → 1/22, 1/23 → 6/23; block D is unaffected).
It reads only; it writes nothing.

**`screening_prompt_proposal.md`** — the write-up that sits on top of all of this: the
settled coding rules, the seven re-coded cases and what they do to the headline numbers,
the proposed replacement prompt, the resolved open questions, and the evaluation design
that requires holding these cases out.

### Still outside this directory

Only two things: the gitignored `cache/llm/` that `build_adjudication.py` pulls abstract
text from, and the script that folded the `hojudge_*` votes into `heldout_truth.json`,
which was never written down — the held-out truth file therefore cannot be regenerated
from what is here.

---

## Known arguable case: F140

`F140` (Brolan et al. 2014, *Globalization and Health*, `10.1186/1744-8603-10-19`) is a FLoRA
entry that the v3.2 screen discards. Reported separately from settled misses, because the
screen and the FLoRA label disagree on an unsettled coding question.

The paper asks what the post-2015 development goals would look like if the method used to
construct the Millennium Development Goals were applied to newer UN targets: it re-applies a
published *method* to new material and contrasts the resulting goals with the HLP's 12.

- **For inclusion** (FLoRA's label): it re-runs an earlier construction methodology and
  compares its output against what the original process produced.
- **For discarding** (the screen's verdict): what is reused is a procedure, not a claim; no
  reported empirical finding is being checked. This is the framework-reuse clause of
  non-qualifying sense 3, added in v3.1 to fix leak pattern P7.

Including it means narrowing that clause — plausibly to reuse *without* comparison against the
original's result — and re-running to confirm the three P7 cases do not return.

Not an abstract-quality artefact: the entry sheet's `abstract_r` for this row is 217 characters,
OpenAlex holds the full 2,323-character abstract, and the screen answers `none` confidently on
both.

### Abstract-source caveat

`flora_positive_cases.json` draws abstracts from the entry sheet's `abstract_r`, while the
pipeline sources them through `search/fetch_abstracts.py` (OpenAlex → Europe PMC → S2 →
CrossRef → Scopus). 22 of the 300 abstracts are under 700 characters and 2 under 400 (median
1,237). Sensitivity measured on this set is a floor, not an estimate; rebuilding it from the
pipeline's own sources would firm it up.

---

## Prompt and truth-set lineage

| file | status |
| --- | --- |
| `prompt_v3.txt` | first spec-written screening prompt; evaluation baseline alongside the production prompt. |
| `prompt_v31.txt` | rule fixes for the leak patterns in `leak_analysis.md`, plus the instrument-boundary ruling. Superseded by v3.2 before it was evaluated; kept because `prompt_v31_diff.md` is the readable statement of those rule changes. |
| `prompt_v32.txt` | v3.1 plus the binary `confident` field. Results in `report_v32.md` and `gate_sweep_v32.md`. |
| `prompt_v33.txt` | **the shipped prompt.** v3.2 plus one sentence in WHAT QUALIFIES (item 7): partial overlap with the original's data does not disqualify a re-test. Results in `report_v33.md`, produced by `score_v33.py` over the two shipped voters under the shipped gate. |

The v3.3 evaluation carries a control the earlier ones did not: `voter_v32r_*` is
`prompt_v32.txt` re-run against the same two models in the same session as `voter_v33_*`.
Neither model is deterministic, so `v32` vs `v32r` is run-to-run variance and `v32r` vs `v33`
is the prompt change. On these 390 cases the two are the same size — see `report_v33.md` §3.

Truth sets: `human_truth.json` (first pass) → `human_truth_revised.json` (the coder's recodes)
→ **`human_truth_v32.json`** (current; 50 `no` / 10 `yes`). Held-out: `heldout_truth.json` →
**`heldout_truth_v32.json`** (current; 20 `no` / 10 `yes`, after the instrument-boundary
ruling). `truth_flips.md` records each flip and each case deliberately left unflipped.
`score_v32.py` and `gate_sweep_v32.py` read the `_v32` files.

Truth labels come from the human coder. A coding ruling changes what the prompt tells models;
it does not re-label a case a coder has decided.
