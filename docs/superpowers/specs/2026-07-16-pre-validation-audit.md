# Pre-Validation Audit — Design Spec
**Date:** 2026-07-16
**Status:** Approved for implementation

---

## Overview

Stage 3 writes `data/extracted.csv`; `extract/csv_to_db.py` then hands the resolved
rows to human validators (Supabase). Today the only gate before validators is
`csv_to_db`'s coarse filter (`filter_status` ∈ replication/reproduction and
`link_method` ∈ resolved methods). That lets through rows that are technically
"resolved" but not fit for a human to judge: a `doi_o` that failed DOI
verification, a row whose `doi_o` equals its own `doi_r` (self-link), a duplicate
`pair_id`, a row with an empty abstract, an outcome quote that was never in the
abstract, and so on. A validator burns time on those, or worse, votes on a record
built on a broken link.

**Pre-validation audit** is a read-only checker that runs over `extracted.csv`
*before* `csv_to_db` and reports per-row problems at two severities:

- **BLOCKER** — the row should not reach a validator at all.
- **WARNING** — the row can go to validators but is flagged for extra scrutiny.

The tool is modelled on `extract/audit_dois.py`: same CLI shape, `log`-based
logging, dry-run-by-default (it never writes to `extracted.csv`; it only writes a
report CSV), a `--doi` single-row mode, and a console summary. It adds one
integration point: `csv_to_db.py` gains an optional `--audit-report PATH` that,
when supplied, drops rows whose `pair_id` carries a BLOCKER in that report.

This is deliberately **not** wired to run automatically inside `csv_to_db`: the
audit is advisory, the operator runs it, reads the summary, and then chooses to
pass the report to the import. Default `csv_to_db` behaviour is unchanged.

---

## Checks

Each firing check emits one report row: `(pair_id, doi_r, check, severity, detail)`.
A single CSV row may produce several report rows (e.g. missing abstract *and* low
link confidence). Group-level checks (duplicate `pair_id`, multi-original rank
consistency) emit one report row per offending CSV row.

### BLOCKER — must not go to validators

| check                  | condition |
|------------------------|-----------|
| `doi_o_unverified`     | `doi_o_verification` not in `{verified, corrected}` (i.e. `mismatch`, `not_found`, `no_metadata`, `no_doi`, `api_error`, `skipped`, or empty). The linked original is unconfirmed. |
| `self_link`            | `clean_doi(doi_o) == clean_doi(doi_r)` and non-empty — the paper is linked to itself. |
| `duplicate_pair_id`    | `pair_id` occurs more than once in the file. Every row of the duplicate group is reported. |
| `unresolved_stage`     | `outcome` ∈ `{pending, api_error}` **or** `link_method` ∈ `{target_pending, api_error, no_original_found}` — the pipeline never finished this row. |
| `missing_display_field`| any of `title_r`, `title_o`, `abstract_r` empty — a validator cannot judge the record without them. `detail` names the missing field(s). |

### WARNING — goes to validators but flagged

| check                    | condition |
|--------------------------|-----------|
| `original_postdates_replication` | `year_o > year_r + 1`. The original should not be published after the replication; tolerance of 1 year absorbs in-press / online-first ordering. Non-numeric years are skipped. |
| `outcome_not_canonical`  | `outcome` not in `OUTCOME_VALUES` imported from `shared/schema.py`. Imported, never hardcoded, so a sibling PR unifying the outcome enum keeps this check correct automatically. |
| `quote_not_in_abstract`  | `out_quote_source == abstract` and `outcome_phrase` is non-empty but is not found in `abstract_r` under a normalized containment check (lowercase, collapse whitespace), with a `rapidfuzz.fuzz.partial_ratio >= 85` fuzzy fallback. The outcome prompt demands a verbatim quote; a quote that is not in the abstract is a fidelity problem. rapidfuzz is already a dependency (`requirements.txt`). |
| `low_link_confidence`    | `link_confidence == low`. |
| `low_outcome_confidence` | `outcome_confidence == low`. |
| `multi_original_inconsistent` | within a `doi_r` group: the `original_rank` values are not exactly `1..n`, or `n_originals` disagrees across the group's rows, or `n_originals` != group size. Papers targeting N originals expand to N rows and must be internally consistent. |

`quote_not_in_abstract` only fires when the quote is claimed to come from the
abstract (`out_quote_source == abstract`); a quote sourced from full text or title
cannot be checked against `abstract_r` and is not penalised.

---

## Severities

- **BLOCKER** is a hard gate: presence of any BLOCKER makes the tool exit `1`, and
  `csv_to_db --audit-report` drops the row.
- **WARNING** never changes exit code or import behaviour on its own. It exists so
  the operator (and, later, the validator UI) can see which resolved rows deserve a
  second look.

The severity of a check is fixed in code (a `(check_name, severity)` pairing), not
inferred, so the summary and the `csv_to_db` gate agree on what "BLOCKER" means.

---

## CLI

```bash
python -m extract.audit_extracted                       # dry-run over data/extracted.csv
python -m extract.audit_extracted --input data/extracted-test.csv
python -m extract.audit_extracted --report /tmp/audit.csv
python -m extract.audit_extracted --doi 10.1037/xyz     # audit one doi_r
```

| flag        | default                          | meaning |
|-------------|----------------------------------|---------|
| `--input`   | `data/extracted.csv`             | CSV to audit |
| `--report`  | `data/pre_validation_audit.csv`  | where the report CSV is written |
| `--doi`     | (none)                           | audit only rows whose `doi_r` matches (cleaned) |

The tool is **read-only** with respect to `extracted.csv` — it only ever writes the
report CSV. Report columns: `pair_id, doi_r, check, severity, detail`.

Console output: a summary table grouped by `(check, severity)` with counts, the
number of rows carrying at least one BLOCKER, and the report path. Exit code `1`
if any BLOCKER fired, else `0`, so the audit can gate a shell pipeline.

---

## Integration with `csv_to_db.py`

`csv_to_db.py` gains one optional flag:

```bash
python -m extract.csv_to_db --input data/extracted.csv --audit-report data/pre_validation_audit.csv
```

- When `--audit-report` is **absent**: behaviour is exactly as today (no change).
- When present: the report is read, the set of `pair_id`s with any BLOCKER-severity
  row is computed, and resolved rows whose `pair_id` is in that set are skipped
  before insert. The count of rows skipped this way is logged separately
  (`skipped by audit`).

The report is treated as advisory input, not a dependency: a missing report path is
a hard error (the operator asked to use it), but the gate only ever *removes* rows —
it can never add rows that `csv_to_db`'s own resolved-mask would not have imported.

---

## Testing

Unit tests build small synthetic DataFrames / CSVs under `tmp_path`:

- Each check fires on a crafted bad row and does **not** fire on a clean row.
- `quote_not_in_abstract` covers exact-substring pass, whitespace/case-normalized
  pass, fuzzy-threshold pass, and a genuine miss; and is skipped when
  `out_quote_source != abstract`.
- Group checks (`duplicate_pair_id`, `multi_original_inconsistent`) fire on the
  right rows and stay silent on consistent groups.
- Exit-code / BLOCKER accounting: a file with only warnings exits `0`; a file with a
  blocker exits `1`.
- `csv_to_db` integration: with a mocked Supabase client, a report that blocks one
  `pair_id` causes exactly that row to be skipped and the rest imported; without
  `--audit-report`, all resolved rows import.

No live API calls; the Supabase client is mocked following
`tests/test_supabase_client.py` patterns.
```
