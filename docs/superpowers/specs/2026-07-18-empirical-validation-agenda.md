# Empirical Validation Agenda — Stages 1–3 (#52, #43, #44)

**Status:** planned (no pipeline code changes yet)
**Owners:** TBD (needs a labeller for the gold sets)
**Tracks issues:** #52 (agenda), #43 (parse scoring), #44 (Stage-2 recall)

## Why this exists

The FLoRA technical report will make quantitative claims about **recall, precision,
and outcome balance**. Today those rest on hand-tuned thresholds and coverage
assumptions that are self-described as unvalidated in code and in `CLAUDE.md`. This
doc turns the open measurement work into a concrete, runnable agenda so the numbers
in the report are measured, not assumed.

It also separates the work into two buckets:

- **Code-only slices** — small fixes that need no data and can ship now as their own PRs.
- **Measurement work** — needs gold sets / hand labelling before any threshold moves.

---

## 1. Gold sets to build (the blocking dependency)

| ID | Set | Size | How sampled | Labels needed |
|----|-----|------|-------------|---------------|
| G1 | Stage-1 recall probe | ~200 | Known replications (data/all_replications.csv) checked for presence in candidates.csv, split by success vs failure phrasing | present / absent, phrasing class |
| G2 | Stage-2 no-phrase bucket | ~300 | Random from the ~2.17M `filter_evidence='no replication phrase detected'` rows | is_replication (y/n) |
| G3 | Stage-3 linking | ~200 | Stratified by `link_method` | doi_o correct (y/n) |
| G4 | Stage-3 outcome | 150–300 | Stratified by decision path incl. bare-`"replicated"`→success bucket | gold outcome |
| G5 | Parse gold PDFs | ~40 | PDFs where the best parse is known/obvious | best method per PDF |

Label storage: `data/gold/<id>.csv` (gitignored; keep a small redacted sample in `misc/`).

---

## 2. Measurements

### Stage 1 — recall (#52)
- Keyword recall from G1, **split by success vs failure phrasing** (the #47 bias check
  in numbers now that S2 mirrors OpenAlex phrases).
- Verify OpenAlex `.search` honours quoted phrases — if not, phrase-precision
  assumptions are wrong. Quick probe: query `"failed to replicate"` vs `failed to
  replicate` and compare result sets.

### Stage 2 — false-rejection rate (#44)
- From G2, estimate the false-rejection (recall-loss) rate of the never-reviewed
  no-phrase bucket. Report as a measured % with CI, not an assumption.
- Compare against the known 21.5% readmission rate on the "phrase-without-citation"
  bucket to bound expected loss.

### Stage 3 — linking precision & thresholds (#52)
- Per-`link_method` precision on G3.
- Threshold sensitivity sweeps for the values currently hard-coded and self-described
  as unvalidated:
  - citation scoring cutoffs (4.0 / 2.0)
  - same-author Jaccard floor (0.05)
  - `doi_verify` Jaccards (VERIFY 0.5, RESOLVE 0.7, TITLE_ONLY 0.6, GAP 1.5)
- `find_all_candidates()` **empty-rate**: measure how often `shared/openalex_client.py`
  returns `[]` (no `openalex_id_r` or no indexed refs) — this bounds rule-based linker coverage.

### Stage 3 — outcome (#52)
- Outcome accuracy on G4, incl. the low-signal bare-`"replicated"`→`success` bucket.
- Abstract-only vs full-text: how often does full text change the coded outcome vs
  abstract alone? (Directly informs the #61 abstract-first-with-escalation design.)

### Stage 3 — parse scoring (#43)
- On G5, measure how often the current score
  `refs×300 + abs_len + intro×2 + min(raw//5, 1000)` picks the known-best method.
- Test the failure mode: does a garbled MarkItDown parse with ~60 regex "references"
  beat a clean GROBID parse? Quantify.
- Re-score candidate weightings and pick weights that maximise agreement with G5.

---

## 3. Code-only slices (no data — can ship now as small PRs)

These were carved out of #43/#44 because they don't depend on gold sets:

1. **#43 inert tie-breaker** — every parser caps `raw_text`, so `min(raw_len//5, 1000)`
   is a flat 1000 for any non-trivial parse and breaks no ties. Remove it or replace
   with a real signal (e.g. section-detection count). Low risk; behaviour only changes
   on genuine ties.
2. **#43 ref-count normalisation** — gate the `refs×300` bonus to structured-metadata
   methods (GROBID / openalex_xml), not the MarkItDown regex, so a noisy parse can't
   manufacture ~60 pseudo-refs. Needs a spot-check but no full gold set.
3. **#44 targeted readmission rule** — re-open Stage-2 rows where an exclusion pattern
   fired **but** a replication phrase **and** an author-year citation are both present
   (the known exclusion-misfire on in-scope computational reproductions). Cheap,
   rule-based, no LLM/embedding pass.

Recommend shipping these three independently of the measurement work.

---

## 4. Deliverables

- `analysis/empirical/<stage>_report.csv` per measurement.
- A short results section per stage feeding the technical report.
- Any threshold change lands as its own PR citing the sweep that justifies it —
  no threshold moves without a measured reason.

## Non-goals

- No threshold is changed in this doc; this is the plan, not the edit.
- The full embedding/LLM second pass over all 2.17M no-phrase rows (#44) is out of
  scope until G2 shows the recall loss justifies the cost.
