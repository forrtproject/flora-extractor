# The gold standard: `data/flora.csv`

The published FLoRA database is the only gold standard this project has. It is the
maintainer's named source and the denominator for every recall claim about the filter.

## Canonical source

```
https://github.com/forrtproject/fred-data/raw/refs/heads/main/output/flora.csv
```

The tracked local copy is `data/flora.csv` (~7.7 MB, `utf-8`), already loaded in
production by `shared/flora_skip.py` to keep Stage 3 from re-extracting papers FLoRA
has. Refresh it by overwriting the local file from that URL and committing the diff:

```bash
curl -L -o data/flora.csv \
  https://github.com/forrtproject/fred-data/raw/refs/heads/main/output/flora.csv
```

A refresh changes the recall denominator, so a monitor report must name the file's
commit (or its sha256) alongside its numbers.

## What one row is

One row is **one replication–original pair**: a published replication study and the
one original study it re-tests. A replication that targets several originals appears
as several rows, which is why the row count exceeds the number of distinct papers.

Measured from the local file on 2026-08-06 (the file was refreshed upstream since
the 2026-08-04 measurement — two new sources appeared and every row now carries a
`doi_r`):

| quantity | count |
| --- | --- |
| rows (replication–original pairs) | 2,504 |
| distinct `doi_r` (replication DOIs) | 1,838 |
| distinct `doi_r` ∪ `alt_identifier_r` | 1,955 |
| rows with a non-blank `doi_r` | 2,504 (none identified by title only) |
| distinct `doi_o` (original DOIs) | 2,321 |
| rows by `type` | replication 2,485 · reproduction 19 |
| rows by `source` | replications 1,518 · COS 716 · openalex 167 · SCORE 88 · reproductions 14 · i4r 1 |

**Identifying columns.** The replication is `doi_r` (primary), `alt_identifier_r` (an alternate
DOI for the same work, e.g. preprint vs version of record) and `title_r`. The original
is `doi_o`, with `alt_identifier_o` and `title_o`. All DOIs must pass `clean_doi()`
(`shared/utils.py`) before comparison — the file mixes bare and URL-form DOIs.
`title_r` is the fallback identifier for any row with no `doi_r` (none in the current
copy; earlier copies had hundreds); match it fuzzily and only as a secondary pass,
never as the primary join.

The copy measured above is sha256 `0e17cc52ff58d4c209038d91c9dc76faf328a46ee51dfb61a11c39b30282753b`.

**Not gold.** The FLoRA *entry sheet* (`data/flora_entry_sheet.csv`, also present as
`data/FLoRA entry sheet - replication list.csv`) is not a gold
standard: it contains unvalidated rows still in flight. Only entries that reached
`flora.csv` are adjudicated. **No LLM-derived corpus may ever be used as a recall
denominator** — a set of papers labelled by a model whose own inputs came through the
keyword filter cannot measure that filter; it measures agreement with itself.

## The recall monitor

Every paper in `flora.csv` is a known-good replication. That makes one question
answerable exactly, with no LLM, no sampling and no labelling cost:

> **For a given routing release, how many known FLoRA papers did rule *X* discard —
> and how many did no rule reach at all?**

A rule that discards a known FLoRA paper is failing — that paper is exactly what the
pipeline exists to find, and a `discard` is terminal. The second half of the question
matters just as much under a rulebook that admits rather than excludes: a known-good
paper that no admitting rule claims is lost by silence rather than by verdict, and the
monitor must count it the same way. Both are the same join; neither depends on how the
rulebook is organised, how many rules it has, or whether it works by exclusion or by
admission.

### Measurement

1. **Build the known-good key set.** From `data/flora.csv`, take `clean_doi(doi_r)` and
   `clean_doi(alt_identifier_r)` over all rows → 1,954 keys covering 2,301 of 2,504 rows.
   Deduplicate: the unit of the monitor is the *paper*, not the pair.
2. **Join against the routed pool** for the release under test, on the pool's DOI key
   (`shared/row_key.py` `primary_key()` ordering: doi first). Report the join rate;
   FLoRA papers absent from the pool are outside the monitor's reach — that is a Stage 1
   coverage question, not a routing failure, and it must be reported separately rather
   than counted as a discard.
3. **Report per rule, over the matched papers only:**
   - `n_known` — known-good papers present in the pool (the real denominator);
   - for each rule id, `n_discarded` — matched papers whose resolved pile is `discard`
     and whose winning `route_rule` is that rule;
   - the same count for shadow rules, from `evaluations`, as the discard the rule
     *would* have made — this is the pre-promotion check for a draft discard rule;
   - `n_admitted_by_pile` — where the surviving known-good papers landed
     (`screen_expensive` / `screen_cheap` / `needs_human` / `pending`), because a paper
     parked in `pending/no_text` is not found either;
   - `n_unclaimed` — matched papers no rule routed anywhere (`pending/no_filter_matched`),
     which is the miss mode of an admission-based rulebook;
   - the DOI list behind every non-zero discard count and behind `n_unclaimed`, so each
     one can be read.
4. **Read.** The headline number is recall: `1 - (papers discarded by any live rule +
   papers unclaimed) / n_known`. Per-rule counts attribute the loss. A rule with a
   non-zero discard count, or a rulebook with a rising `n_unclaimed`, is a regression to
   fix before the next release ships.

### What it can and cannot say

It bounds **recall loss on the genre FLoRA already contains** — heavily psychology and
social science, weighted by what the database's own sourcing found (`source`:
replications / COS / SCORE). It says nothing about recall on genres FLoRA under-covers,
and nothing at all about **precision**: papers outside `flora.csv` are not known
negatives, they are unlabelled. Precision still needs someone to read a sample of what
a rule discards.

Because `flora.csv` rows also feed `shared/flora_skip.py`, these papers are deliberately
skipped downstream. That is irrelevant to the monitor, which measures routing, not
extraction — but a monitor implementation must read the routing table directly rather
than anything downstream of the skip list.

Not implemented here. This file is the specification; the implementation belongs with
the engine's diagnostics (`python -m filter.engine diagnose`).
