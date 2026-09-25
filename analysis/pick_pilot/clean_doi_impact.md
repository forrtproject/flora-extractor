# `clean_doi()` — what normalising `//` and `%xx` would change (handover step 4)

Counted 2026-09-23 by `.venv/bin/python -m analysis.pick_pilot.clean_doi_impact`
(raw table: `clean_doi_impact_counts.md`). Nothing was changed; `clean_doi()` is as it
was. The candidate change: `10.xxxx//…` → `10.xxxx/…`, and URL-decode (`%3c` → `<`).

## Counts

| Artifact | Column | DOI cells | would change | `//` | `%xx` |
| --- | --- | ---: | ---: | ---: | ---: |
| extracted.csv | `doi_r` | 3,550 | 8 | 8 | 0 |
| extracted.csv | `doi_o` | 4,141 | 23 | 23 | 0 |
| set-aside CSVs (9) | `doi_r` | 2,821 | 6 | 6 | 0 |
| set-aside CSVs (9) | `doi_o` | 610 | 5 | 5 | 0 |
| flora.csv | `doi_r` | 2,299 | 1 | 1 | 0 |
| flora.csv | `doi_o` | 2,487 | 7 | 5 | 2 |
| flora.csv | `alt_identifier_r` | 154 | 0 | 0 | 0 |
| FLoRA entry sheet | `doi_r` | 3,121 | 0 | 0 | 0 |
| FLoRA entry sheet | `doi_o` | 3,820 | 26 | 25 | 1 |
| validated_skip.csv | `doi` | 1,769 | 14 | 14 | 0 |
| Supabase `unvalidated` | `doi_r` | 3,040 | 9 | 9 | 0 |
| Supabase `unvalidated` | `doi_o` | 3,689 | 26 | 26 | 0 |
| Supabase `validated` | `doi_r` | 327 | 1 | 1 | 0 |
| Supabase `validated` | `doi_o` | 347 | 1 | 1 | 0 |

Identity effects:

- **pair_id** = md5(`doi_r|doi_o`) as stored. 27 of the 3,550 `extracted.csv` rows
  whose pair_id has that form would get a new pair_id if re-written, 6 set-aside rows,
  and **29 rows already in Supabase `unvalidated`**. Stored payloads are rendered as
  stored, so pair_ids move only when a work is re-extracted (e.g. the `llm_references`
  redo, step 3.5) — but then a re-exported row would reach `csv_to_db.py` under a new
  pair_id beside the queued one, unless flora-validation retires the old record.
  (`validated` holds no pair_id column that matches this form, so nothing moves there.)
- **Skip lists.** The FLoRA skip set (2,324 DOIs) gains no match: no row of ours misses
  it only because of the spelling. The validated skip list gains **2** (one is
  `10.1037//0021-843x.108.3.532`, an `llm_author_year_search` row): works already in
  the validation tables whose rows we still ship because the spellings differ.
- `%xx` occurs only on FLoRA's side (3 `doi_o` cells — the Wiley SICI DOI
  `10.1002/(sici)1099-0771(199806)11:2%3c107::…`), none in anything we write.
- Resolution: `https://doi.org/10.1037//0022-3514.69.4.603` answers 301 to the
  single-slash form, so the collapsed spelling is the canonical one and is safe to send
  to APIs. (Crossref rate-limited the probe, so its own record was not read.)

## Decision (2026-09-25)

Lukas chose option 2's first half: `clean_doi()` itself now collapses `//` and decodes
`%xx` (only into a valid DOI with no `%` left, so it stays idempotent), and the export
re-spells stored payloads at render (`_canonical_dois()` in `extract/tier.py`), so the
old pair ids retire as `superseded` through the manifest rather than by a
flora-validation migration. The recommendation below is what was weighed.

## Recommendation (superseded by the decision above)

**Do not change `clean_doi()` itself now.** The gain is 2 skip-list matches; the cost is
29 queued validation records whose pair_id would move the next time their work is
re-extracted — and the step 3.5 redo re-extracts exactly that kind of work. `clean_doi`
is called at ~150 sites and its output is also written into rows and cache keys, so a
change there is a data migration, not a bug fix.

Instead, either:

1. **Comparison-only normalisation** (preferred): a `doi_match_key()` beside
   `clean_doi()` that also collapses `//` and URL-decodes, used where DOIs from two
   SOURCES are compared — `shared/flora_skip.py`'s two loaders and the export's skip
   filter (`extract/export.py`), plus the Observatory/FLoRA joins in analysis. That
   picks up the 2 validated matches and moves no pair_id and no cache key.
2. Or change `clean_doi()` together with a flora-validation migration that rewrites the
   29 `unvalidated` pair_ids (and 1 `validated` row's DOIs) in the same step — only
   worth it if `//` DOIs are to be removed from stored rows for their own sake.

The 3 FLoRA `%3c` cells are a data-owner fix (already in the sheet sent 2026-09-23).
