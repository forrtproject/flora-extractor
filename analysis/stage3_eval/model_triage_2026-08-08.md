# Second-model triage of the provisional link classes — 2026-08-08

A cross-vendor check of the three quarantined link classes before the first
production campaign: nine Claude Sonnet agents (local subscription, no API bill)
independently adjudicated every provisional/disputed link on disk, ~30 rows each,
with Crossref lookups where the row's own fields could not settle it. This is triage
for the human validation pass, not a substitute for it: per-row verdicts are in
`model_triage_2026-08-08.csv` so validators can start from the flagged rows.

## What was checked

All 265 links across the three classes, from the two places they currently live:

| class | source | links | judged wrong |
| ----- | ------ | ----: | -----------: |
| `llm_title_search` | production `data/provisional_title_search.csv` | 39 | 3 |
| `llm_title_search` | sandbox evaluation batches | 65 | 1 |
| `llm_author_year_search` | sandbox evaluation batches | 160 | 1 |
| `keyed_link_disputed` | sandbox (work 3124119366) | 1 | 1 — concurs with the pipeline's own dispute |

One Sonnet "wrong" was reversed on a Crossref check: work 6963478887's link to
`10.1177/0261927x01020004004` was flagged because Crossref carries only the
truncated main title ("Name Your Favorite Musician"); the DOI is Stahlberg, Sczesny
& Braun (2001), the generic-masculine paper the multi-lab replication targets. The
counts above are after that reversal.

## The five genuinely wrong links

- `10.1167/6.6.859` → `10.1017/s1138741600006259` (production): linked to Hutchison
  & Loomis's 2006 *reply* to Proffitt et al., not the targeted original.
- `10.1080/10632913.2015.1007405` → `10.1080/07303084.1998.10605608` (production):
  the 1998 NAEP arts report, not the 2008 assessment cycle the study replicates.
- `10.1111/j.0000-0000.2001.00194.x` → `10.25291/vr/25-vr-40` (production): an
  Australian legal case report ("Edwards v Edwards"), not the Edwards
  two-presidencies article.
- work 3091730662 → `10.1108/arj-09-2020-0312` (sandbox): another paper *about*
  replicating Levi, Li & Zhang (2014), not the original.
- work 7160676754 → `10.1016/j.jarmac.2014.04.005` (sandbox): Nahari & Vrij's
  alibi-witness paper — no Fisher on it — not the Nahari, Vrij & Fisher (2014)
  information-protocol study.

## The 200-row experiment: does a second same-model pass add anything?

Before promoting the search classes into the validation import, the maintainer asked
whether an extra LLM check would help — and doubted it, since the pooled pick already
offers "none" first-class. Measured on 200 fresh production rows (158 author-year, 42
title-search, sampled seed 20260808 from rows no earlier triage had seen), two checks
ran side by side:

- **The same-model confirm pass** (`confirm_keyed_original`, the measured issue #186
  prompt, `gpt-5.4-mini`): **0 flags in 200** — it passed both rows Sonnet proved
  wrong. When the same model has already accepted a pick from the same evidence, a
  second cold read of that evidence agrees with it. It is therefore NOT wired for
  the search classes; do not re-propose it. (It stays wired for the keyed-record
  path, where it was measured catching a cross-namespace mis-pick.)
- **Sonnet with Crossref lookups: 198/200 correct.** Both wrongs needed external
  metadata: work `10.17605/osf.io/fra9x` linked an adjacent Peterson
  selective-exposure paper instead of Peterson & Iyengar (2021), and
  `10.17605/osf.io/azb3g` linked the PsycTESTS measure record `10.1037/t36758-000`
  instead of Bastian et al. (2012).

Consequences shipped: the two classes are promoted to RESOLVED (outcome-coded,
imported at low confidence, ladder 18); a PsycTESTS-shaped DOI in the candidate pool
now carries a flag (a flag and not a drop — `flora.csv` itself records t-DOIs as
`doi_o` on 3 curated rows); and the two wrong rows were corrected by supersede with
Crossref-verified DOIs (Bastian → `10.1177/0146167211424291`, Peterson →
`10.1111/ajps.12535`).

## Reading

- Every wrong link sits in a quarantine file that is never imported for validation,
  which is the design working: the provisional classes exist so a human discards
  these five instead of the database inheriting them.
- The sandbox pooled resolver's rates — 1 wrong of 66 title-search links (98%), 1 of
  160 author-year links (99%) — are far above the ~50% the pre-pooling title search
  measured, consistent with the iteration-16 evaluation.
- The production title-search file (3 wrong of 39) predates the pooled resolver;
  those rows reopen at generation 17 and re-resolve under it in the campaign.
- The one `keyed_link_disputed` row was independently called wrong by a different
  vendor's model — a second opinion agreeing with the issue #186 check that demoted
  it.
