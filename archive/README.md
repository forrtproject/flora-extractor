# archive/

Superseded artifacts kept for the record. Nothing here is read by the pipeline,
the tests, or the docs; paths mirror where each item used to live. The reports
that summarise these artifacts stay in their original locations (e.g.
`analysis/screening_eval/report_v33.md`, `analysis/prescreen_eval/REPORT.md`,
`analysis/stage3_eval/REPORT.md`) — this directory holds the raw material behind
them, plus one-off tooling that already served its purpose.

| Path | What it is | Why archived |
| ---- | ---------- | ------------ |
| `analysis/screening_eval/voter_*.json` (40) | Per-prompt/per-model screening run records (v2–v33 generations) | Their numbers live in `report_v3/v32/v33.md`; re-runnable spend |
| `analysis/screening_eval/report_v3.md`, `report_v32.md`, `prompt_v2/v3/v31/v32.txt`, `prompt_v31_diff.md`, `truth_flips.md`, `leak_analysis.{md,py}`, `gate_sweep.md`, `screening_prompt_proposal.md`, `eval_v3.py`, `score_v3.py`, `score_v32.py`, `gate_sweep.py` | Superseded prompt generations, their eval scripts and reports, and the pre-ship prompt proposal (validated in-sample on derivation cases) | Production is v3.3; `report_v33.md`, `prompt_v33.txt` and `gate_sweep_v32.{md,py}` remain in place as the cited evidence |
| `analysis/prescreen_eval/pre_p*.json` (45) | Per-prompt/per-model pre-screen run records | Summarised in `analysis/prescreen_eval/REPORT.md` and `OVERRIDE_EVAL.md` |
| `analysis/stage3_eval/payloads-*.md` (9) | Rendered prompt payloads for human reading during the Stage 3 eval | Regenerable via `analysis/stage3_eval/read_batch.py`; referenced only by `REPORT.md` |
| `analysis/stage3_eval/campaign.sh` | One-shot 2026-08-07 campaign runner with a hardcoded local path | Campaign complete; documented in `REPORT.md` |
| `analysis/rescan_filter_gate_impact.csv` | Output of the removed `rescan_impact_report.py` (deleted 2026-08-06) | Generating script is gone |
| `analysis/provenance/fulltext_provenance_audit.csv` | One-shot 2026-08-04 audit of fulltext provenance | No generating script in the repo; superseded by the `pdf_source`/`parse_method` columns |
| `redesign/coding_app_30_discards.html`, `coding_app_heldout4.html` | Blind coding apps for the screening eval | Results captured in `analysis/screening_eval/flora_coding_v3_results.csv` and the reports |
| `redesign/screening_prompt_spec.md` | Task spec the screen prompt was written from | Shipped wording lives in `shared/prompts.py` (`_CLASSIFY_PROMPT`) |
| `redesign/builders/` | Scripts + fragments that built `screening_audit.html` and the coding apps | Their outputs are frozen; the kept `redesign/*.html` carry the comment threads |
| `tools/data/*_2026-07-22.sh` | Dated `gh issue close`/`comment` batch scripts | Ran once on 2026-07-22 |
| `current-state.html`, `handover-next-steps.html` | Root status snapshots (2026-08-05/06) with comment overlays | Superseded status; CLAUDE.md is the current state of record |
| `redesign/current_state{,_v3,_v4}.html`, `screening_audit.html`, `rulebook_review.html` | Redesign state-of-play, audit and review pages, each with its own comment-overlay slug | The redesign shipped; threads stay reachable by opening the files here. `redesign/rulebook_v2.html` stays in place — live `filter/spec/*.json` cite it |
