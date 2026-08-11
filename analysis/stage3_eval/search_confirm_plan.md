# Calibrating the search-link confirmation grades

The graded confirm (`confirm_search_original` in `shared/llm_client.py`, wired
observe-only by `_confirm_search_row` in `extract/run_extract.py`) annotates every
accepted `llm_title_search` / `llm_author_year_search` link with one of four grades —
`clearly_target`, `likely_target`, `unlikely_target`, `clearly_not_target` — appended
to `link_evidence` as a `search_confirm:` segment. It decides nothing. This document
says how the grades become a decision, or get retired.

## Why graded, and why observe-only

The binary form of this check was measured and rejected: over 200 fresh search-class
rows it flagged nothing, and both known-wrong links passed
(`model_triage_2026-08-08.md` — "It is therefore NOT wired for the search classes; do
not re-propose it"). Both wrongs needed external CrossRef metadata to detect, which a
cold same-evidence call does not have. The graded form exists because the maintainer
wants calibration room ("more likely than not" vs "clearly not") rather than a hard
confident-no gate — and because whether the grades carry ANY signal on this class is
exactly what must be measured before anything gates on them.

## Collection

The grades accumulate for free: every live or sandbox run over the low-confidence
search class writes them into `link_evidence`, cached under the `searchconfirm`
prefix, and they reach `data/extracted.csv` through the stored payloads. The full
class is ~1,080 rows; one campaign pass covers it.

## The calibration, in order

1. **The kill test.** Cross the grades against the five known-wrong links in
   `model_triage_2026-08-08.csv`. If the wrong links do not sit visibly lower than
   the correct ones, the check has no signal on this class — retire it rather than
   tune it, per the triage's own conclusion about the binary form.
2. **Distribution read.** Measured precision of the class is 98–99%, so
   `clearly_not_target` should be rare (order of 1–2%). A materially higher rate is
   itself a finding — either the prompt is miscalibrated or the class drifted.
3. **Threshold decision.** Only after 1 and 2: decide which grades gate what. The
   expected shape, mirroring the keyed check (`_confirm_keyed_row`):
   `clearly_not_target` → the disputed queue (`keyed_link_disputed.csv` pattern —
   settle, quarantine, human decides); `unlikely_target` → flag only (a note in
   `link_evidence`, `link_confidence` stays low); the two positive grades → nothing.
4. **On enforcement**, add `build_search_confirm_prompt` to `_GENERATION_PROMPTS` in
   `extract/tier.py` — from that day an edit to the prompt changes what a row
   concludes and must reopen the works it decided. The comment there marks the spot.

## Evidence base

- `model_triage_2026-08-08.md` — the class's measured precision and the binary
  check's failure on it.
- `keyed_confirm_eval.py` / issue #186 Shape 1 — the measured pattern the enforcement
  shape borrows (1 catch, 0 false positives over 63 keyed links).
