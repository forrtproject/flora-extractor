# `adjudication/` — who is right where FLoRA and the Observatory disagree

Blinded two-judge adjudication of the divergences between the Metascience Observatory
(`../replications_database_2026_09_04_184008.csv`, gitignored) and (a) our Stage 3 rows
(`data/extracted.csv`), (b) shipped FLoRA (`data/flora.csv`). Read-only against every
pipeline artifact. The findings and the next steps are in
`docs/handover-observatory-campaign-2026-09.md`.

`packets/` (the papers' full text) and `out/` (raw judge answers, Crossref cache, replay
cache) are gitignored and live only on the machine that ran them.

## Scripts, in run order

| Script | Does | Writes |
| --- | --- | --- |
| `mo_vs_flora.py` | Observatory vs FLoRA on the replications both hold, excluding the Observatory rows imported FROM FLoRA | `mo_vs_flora.csv` |
| `agreement.py` | Our Stage 3 rows vs the Observatory, per WORK, from the raw Observatory file (it stores one row per effect/lab — `priority.csv` kept one arbitrary row per paper) | `works.csv`, `pairs.csv` |
| `build_sample.py` | Stratified blinded packets (A/B random) for the pipeline divergences; `--complete-original` adds every remaining different-original work | `packets/`, `key.csv` |
| `build_flora_sample.py` | The same for FLoRA vs the Observatory | `packets/flor-*`, `key_flora.csv` |
| `run_codex.sh` | One Codex judgement per packet (`CODEX_MODEL`, `CODEX_OUT`, `PACKET_GLOB`) | `out/codex*/` |
| *(Claude judges)* | Agent-tool subagents, ~13–15 packets each, told `judge_prompt.md` + `schema.json` and not to open `key*.csv` or `out/` | `out/claude/` |
| `build_report.py` | Unblinds, tallies, renders the report | `verdicts.csv`, `report.html` |
| `doi_pairs.py` | Same-title-different-DOI pairs → MISTAKE (erratum, reply, issue, review, other paper, malformed) vs ALTERNATIVE IDENTIFIER (preprint, working paper, duplicate registration) | `doi_pairs.csv` |
| `build_decisions.py` | One recommended action per divergent work, both populations | `decisions.csv`, `decisions.html` |
| `flora_corrections.py` | The verified doi_o corrections for FLoRA's data owner | `flora_corrections.csv` (→ the Google Sheet) |
| `replay_pick.py` | Replays the cached `llm_references` prompts with another linking model and scores it against the judged labels | `replay_<model>.csv` |

Judges: Claude (Opus 5.5) + Codex `gpt-5.6-sol` for the 169 pipeline items; Claude +
Codex `gpt-6-sol` for the 26 FLoRA items. Position bias checked (first answer chosen
43% / 51% when decisive); inter-judge agreement 76%.
