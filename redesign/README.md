# Redesign — the screening rewrite and its review artefacts

Working folder for the Stage-3 screening redesign. The evaluation evidence lives in
`analysis/screening_eval/`; this folder holds the deliverables and the pages used to review
them.

| file | what it is |
| --- | --- |
| `current_state.html` | State-of-play report for the pipeline: where the data stands, what to trust, the decisions needed, the proposed prompts (§8, with the implementation checklist in §8.1.1), and every production prompt as it runs today (§9). Carries a comment overlay — reviewers select text to comment or suggest edits. |
| `screening_prompt.txt` | The validated front-door screening prompt. Copy of `analysis/screening_eval/prompt_v32.txt`, which stays the source of truth for the evaluation. |
| `screening_prompt_spec.md` | The task specification the prompt was written from: what the screen must decide, the output schema, and every coding rule, independent of wording. |
| `coding_app_30_discards.html` | Blind coding app for the 30-case review of fresh discards. Results: `analysis/screening_eval/flora_coding_v3_results.csv`. |
| `coding_app_heldout4.html` | Blind coding app for the four held-out instrument-boundary cases. |

Both coding apps are self-contained: open in a browser, code with the keyboard, download a CSV.
Nothing is sent anywhere and no model verdicts are embedded.

## Status

The screening prompt is validated: 84% of hard negatives discarded with zero missed positives
(89% under the softened gate), against 61% for the production prompt. Numbers and method in
`analysis/screening_eval/report_v32.md`. Implementation is pending final review — the checklist
in §8.1.1 of `current_state.html` covers the prompt swap, the schema and gate changes, the
`record_type` migration, the Stage 2 LLM filter retirement, and the cache consequences.
