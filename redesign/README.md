# Redesign — the screening rewrite and its review artefacts

Working folder for the Stage-3 screening redesign. The evaluation evidence lives in
`analysis/screening_eval/`; this folder holds the deliverables and the pages used to review
them.

| file | what it is |
| --- | --- |
| `current_state.html` | **Historical.** The first state-of-play report (August 2026): where the data stood, the decisions needed, the proposed prompts (§8, implementation checklist §8.1.1), and the production prompts as they ran then (§9). Its content is superseded, but it carries a live comment overlay with reviewer threads on it — **do not delete it**, the threads are reachable only through this file. |
| `current_state_v2.html`, `current_state_v3.html` | Later generations of that report, each with its own comment layer. Also superseded. |
| `current_state_v4.html` | **Current.** The 2026-08-04 state-of-play report; supersedes v1–v3. Read this one. |
| `screening_prompt_spec.md` | The task specification the prompt was written from: what the screen must decide, the output schema, and every coding rule, independent of wording. The shipped wording is not here — it is `_CLASSIFY_PROMPT` in `shared/prompts.py`. |
| `coding_app_30_discards.html` | Blind coding app for the 30-case review of fresh discards. Results: `analysis/screening_eval/flora_coding_v3_results.csv`. |
| `coding_app_heldout4.html` | Blind coding app for the four held-out instrument-boundary cases. |
| `screening_audit.html` | The screening pipeline audit (July 2026): how the pipeline actually behaves step by step, the LLM call map, bugs and proposed fixes, intent-vs-code drift, and the settled screening rules. The historical record `current_state.html` builds on; carries its own comment layer under a separate project slug, so its review threads stay reachable through this file. |
| `builders/` | Generators: `assemble_audit.py` rebuilds the audit page from its fragments, `build_coding_app.py` rebuilds a coding app from a sheet. The coding sheets and the model key for the 30-case review live here too. |

Both coding apps are self-contained: open in a browser, code with the keyboard, download a CSV.
Nothing is sent anywhere and no model verdicts are embedded.

## Status

**Shipped and merged.** The redesign is in production on `main`: the branch
`redesign/screen-swap-8.1` is merged and the §8.1.1 checklist is done. The screen is the
five-field v3.2 schema parsed in `shared/llm_client.py`, the G-softqual gate is
`screen_gate()` (one definition, called from both the front door and the batch-tools path),
`record_type` and `screen_categories` come from the screen, voter 2 is `gpt-5.4-mini` on
OpenAI direct, and the old Stage 2 LLM filter is gone (PR #152 replaced Stage 2 entirely
with the filter engine).

**Production runs prompt v3.3**, not the v3.2 this folder was written against: v3.2 plus
the partial-overlap rule. The shipped wording is `_CLASSIFY_PROMPT` in
`shared/prompts.py`; the evaluated copy is `analysis/screening_eval/prompt_v33.txt` and
the evidence is `analysis/screening_eval/report_v33.md`, with `report_v32.md` behind it.
Quote v3.3's numbers, not the v3.2 ones this section used to carry.
