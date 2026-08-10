# Redesign — the screening rewrite and its review artefacts

Working folder for the Stage-3 screening redesign. The evaluation evidence lives in
`analysis/screening_eval/`; this folder holds the deliverables and the pages used to review
them.

| file | what it is |
| --- | --- |
| `rulebook_v2.html` | The Stage-2 rule book v2: the whitelist proposal and the measurements behind it. **Live provenance** — four specs in `filter/spec/` cite it by name in their `measured` blocks (proposals SS1, SS1.2, SS1.3), as do `filter/spec/rule_ideas.md` and `docs/filter-engine.md`. No comment layer on this one. |

## Moved to `archive/redesign/`

Superseded working material, kept for the record — see [`archive/README.md`](../archive/README.md).
The HTML pages carry their comment overlays with them: each page's reviewer threads are
keyed to its own project slug and stay reachable by opening the file from `archive/` —
which is why these pages are archived rather than deleted.

| file | what it is |
| --- | --- |
| `archive/redesign/current_state.html` | **Historical.** The first state-of-play report (August 2026): where the data stood, the decisions needed, the proposed prompts (§8, implementation checklist §8.1.1), and the production prompts as they ran then (§9). Superseded, but its comment overlay's reviewer threads are reachable only through this file. |
| `archive/redesign/current_state_v3.html` | A later generation of that report, with its own comment layer. Also superseded. |
| `archive/redesign/current_state_v4.html` | The 2026-08-04 state-of-play report, last of the lineage. Superseded by the shipped state described in CLAUDE.md. |
| `archive/redesign/rulebook_review.html` | The reviewed rule book: the same proposal with reviewer comment threads on it, under its own project slug (`flora-extractor-rulebook-review`), so the threads are reachable only through this file. |
| `archive/redesign/screening_audit.html` | The screening pipeline audit (July 2026): how the pipeline actually behaves step by step, the LLM call map, bugs and proposed fixes, intent-vs-code drift, and the settled screening rules. Carries its own comment layer under a separate project slug. |
| `archive/redesign/screening_prompt_spec.md` | The task specification the prompt was written from: what the screen must decide, the output schema, and every coding rule, independent of wording. The shipped wording is not here — it is `_CLASSIFY_PROMPT` in `shared/prompts.py`. |
| `archive/redesign/coding_app_30_discards.html` | Blind coding app for the 30-case review of fresh discards. Results: `analysis/screening_eval/flora_coding_v3_results.csv`. |
| `archive/redesign/coding_app_heldout4.html` | Blind coding app for the four held-out instrument-boundary cases. |
| `archive/redesign/builders/` | Generators: `assemble_audit.py` rebuilds the audit page from its fragments, `build_coding_app.py` rebuilds a coding app from a sheet. The coding sheets and the model key for the 30-case review live here too. |

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
the evidence is `analysis/screening_eval/report_v33.md` (earlier generations under
`archive/analysis/screening_eval/`).
Quote v3.3's numbers, not the v3.2 ones this section used to carry.
