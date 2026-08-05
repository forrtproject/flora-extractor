# OSF registrations: the template census, and why the discard stays in shadow

Run 2026-08-04. Population: every row of the survivor pool on the OSF registrant
`10.17605` with no abstract — **25,819 rows on the registrant, 3,016 without text**,
counted on disk by `build_worklist.py`, not read off any code path.

The question. `filter/spec/osf-registration-protocol.json` proposes to discard OSF
registrations whose template is neither post-completion nor an Open-Ended Registration
carrying the replication stem. It shipped `shadow: true` because that is a **negation
over a vocabulary nobody had enumerated** — true of the eight templates a 60-row sample
showed, unverified beyond them. This is the enumeration, plus the read that the
promotion recipe asks for (`filter/spec/CONVENTIONS.md`, "Promoting a rule out of
shadow").

**Outcome: not promoted.** The recall gate passes exactly; the read does not.

## What the fetch found

`python -m filter.engine.backfill --source osf` resolved all 3,016. **1,674 are
registrations**; the other 1,342 are OSF projects or components, which the registrations
endpoint 404s on — they get no text, stay `pending/no_text`, and neither rule touches
them.

| verdict (the shipped specs, run by the engine's own evaluator) | rows | known FLoRA |
| --- | --: | --: |
| admit — `osf-registration-completed` | 366 | **4** |
| discard — `osf-registration-protocol` | 1,308 | **0** |
| untouched — no registration text | 1,342 | 4 |

Thirteen templates appeared. Every one outside the keep arms is a pre-data-collection
form, which is the claim the shadow existed to test:

| template | verdict | rows | FLoRA |
| --- | --- | --: | --: |
| OSF-Standard Pre-Data Collection Registration | discard | 536 | 0 |
| Open-Ended Registration | admit | 336 | 0 |
| Replication Recipe (Brandt et al., 2013): Pre-Registration | discard | 178 | 0 |
| Preregistration Template from AsPredicted.org | discard | 155 | 0 |
| OSF Preregistration | discard | 139 | 0 |
| Prereg Challenge | discard | 115 | 0 |
| Open-Ended Registration (no replication stem) | discard | 57 | 0 |
| EGAP Registration | discard | 54 | 0 |
| Pre-Registration in Social Psychology (van 't Veer & Giner-Sorolla, 2016) | discard | 49 | 0 |
| **Replication Recipe (Brandt et al., 2013): Post-Completion** | **admit** | **30** | **4** |
| Registered Report Protocol Preregistration | discard | 22 | 0 |
| Qualitative Preregistration | discard | 2 | 0 |
| Replication Recipe (Brandt et al., 2014): Pre-Registration | discard | 1 | 0 |

Three things follow directly.

**The recall gate passes, exactly rather than by sample.** Zero known FLoRA papers are
discarded, over the whole population. The population holds **16** known FLoRA papers —
not the 10 the earlier 60-row estimate implied, and not the 8 a DOI-only match finds
(see "FLoRA names OSF records by URL" below). Five are admitted; eleven are 404s with no
registration record and are untouched by either rule.

**No post-data-collection template exists here at all.** The keep arm was drafted to
cover them and the arm was dropped on the maintainer's ruling (2026-08-04) that
registering after collection still registers a design. The census settles it: there was
never anything to lose.

**The Open-Ended arm is admitting the Reproducibility Project: Psychology, which FLoRA
already covers.** 336 of the 393 Open-Ended rows are RPP registrations — "Replication of Janiszewski & Uy (2008, PS,
Study 4b)", whose entire text is "Registered prior to RPP publication". The `replicat*`
marker fires on the OpenAlex **title** in 99% of them, not on the responses form. The
arm works, but not by the route its spec described, and that wording is corrected.

What sits behind it is a scope question, not a rule question. FLoRA **does** hold the
RPP results — 92 rows, one per replicated original, 55 failed / 34 successful / 3 mixed,
`source = COS` — but keyed to the aggregate paper, with the individual report reachable
only through `url_r`. Of the 336 registrations this arm admits, 95 parse as
"Replication of <Author> (<year>…", and **42 of those 95 match an original FLoRA already
lists under the RPP paper** (first-author family name + year). Admitting them produces a
second record for a replication FLoRA already has, unless the individual RPP report is
wanted as a record in its own right — which is a FLoRA data-model call for the
maintainer. Note the guid fix above does NOT catch these: FLoRA's `url_r` names the RPP
*report* page, the pool row is the *registration*, and the two have different GUIDs.

## FLoRA names OSF records by URL, and our matching did not

Found while checking whether the Reproducibility Project belongs in scope, and it is a
production bug rather than a fact about this census. Over `flora.csv`:

| how FLoRA identifies an OSF record | rows |
| --- | --: |
| an `osf.io/<guid>` URL in `url_r` / `url_o` / `oa_url_*` | **366** |
| an OSF DOI (`10.17605/…`) in `doi_r` / `doi_r_alt` | 51 |
| both | 9 |

So ~357 OSF records FLoRA already holds are invisible to any DOI-keyed matcher. Every
GUID resolves to exactly one DOI (`10.17605/OSF.IO/<guid>`), which is the form the pool
carries, so the two spellings meet by construction — `_osf_doi_keys()` in
`shared/flora_skip.py` is now the one place that mapping lives, and both the skip list
and this census read it. The effect here: 16 known FLoRA rows in the population rather
than 8. **The discard count is 0 under either matching**, so nothing below changes.

The RPP rows are what exposed it. All 92 carry the aggregate Science paper in `doi_r`
and `title_r`, and the individual replication's OSF page only in `url_r` — 91 distinct
`https://osf.io/<guid>` URLs. Stage 3's skip list read `doi_r` alone, so every one of
them could be extracted and pushed to validation a second time.

## The read: 300 of the 1,308 discards

Drawn uniformly at random (`random_state=17`, `review_sample.csv`), read by six Sonnet
agents over the registration's own text, truncated at 3,000 characters. Each was asked
only whether the record reports results of a replication already run, told to judge from
the text alone, and told to err toward keeping. Labels come from the record, nothing
downstream of the rule. `review_verdicts.csv` lists every non-`plan` verdict; the 259
sampled rows not listed there were all labelled `plan`.

| | n |
| --- | --: |
| plan — an intended or in-progress study | 259 |
| unclear — cannot tell, usually a near-empty record | 36 |
| flagged as reporting results | 5 |

I read all five. Adjudicated:

- **`10.17605/osf.io/pr8a4` — a real loss.** "Replication with Registration: Examining
  Kerner's 'What We Talk About When We Talk About FDI'", on the EGAP template. Its text
  is a completed replication write-up in past tense: *"In Table 1 I replicate models 1-3
  from Kerner using robust regression… I replicate models 1-3 from Kerner using the
  xtabond command in Stata 11 and present these results in Models 4-6 in Table 2."* The
  rule would delete a finished replication of a named study. Note what does **not** save
  it: the record's own structured field q8 says "Registration prior to researcher access
  to outcome data", contradicting its content — so no timing field can be trusted to
  catch this class.
- `10.17605/osf.io/6me7j` — reports results (q10: "Registration following analysis of
  the data") but is a botany study of *Allium wallichii* pollination. A completed study,
  not a replication; out of FLoRA scope, so discarding it loses nothing.
- `10.17605/osf.io/a94um`, `10.17605/osf.io/7f2tc` — over-calls. Both state an outcome
  belonging to a **prior** study (a class project; the van 't Veer template's
  prior-work field), while the registration itself is prospective.
- `10.17605/osf.io/53ysw` — an over-call from an inference; the record's entire text is
  one sentence naming a journal acceptance and states no result.

## Why this stops the promotion

One real study in a clean-sample-of-300 is a point estimate of **V/n ≈ 1,308/300 ≈ 4.4
papers** lost to this one rule, against a database holding ~1,500 replication papers.
CONVENTIONS is explicit: *"Any real study in the sample → do not promote. Narrow the
rule and start again."* The 36 `unclear` rows push the same way — most are records whose
entire content is `looked: No`, and a rule that deletes what nobody can read is exactly
the case the evidence gate exists for.

The recall gate passing is not a substitute. `flora.csv` is what FLoRA has already
found; the Kerner replication is precisely the kind of record that is *not* in it yet,
which is the reason to be collecting these at all.

## The RPP reports have no DOI, and that is the blocker

Checked 2026-08-05 because the maintainer asked for the OSF report DOIs to be added to
FLoRA (`alt_identifier_r` in the COS source sheet, which `prepare_flora.qmd` renames to
`doi_r_alt`). They do not exist:

| | n |
| --- | --: |
| FLoRA RPP `url_r` report nodes | 91 |
| **with a DOI of their own** | **0** |
| with at least one OSF *registration*, which does have a DOI | 83 |
| … exactly one registration | 46 |
| … two to four | 37 |
| with no registration at all | 8 |

`/v2/nodes/<guid>/identifiers/` is empty for all 91, and `10.17605/OSF.IO/SU6BM` and
`…/XSE7Q` both 404 at DataCite. OSF mints a DOI automatically for a registration or a
preprint, not for a project page — so a constructed `10.17605/OSF.IO/<node-guid>` would
look like a DOI and resolve nowhere.

What does exist is the registration DOI, reachable from the report node through
`/v2/nodes/<guid>/registrations/`. Ten of ten sampled resolve at DataCite. The full
mapping is `rpp_report_to_registration.csv` (built by `rpp_report_identifiers.py`), and
it is one-to-many for 37 nodes — `append_alt_identifier()` in fred-data takes a
comma-separated list, so that shape fits the column natively.

It is not the same object, and the difference has to survive into whatever is written: a
registration is the pre-registered snapshot (125 of the 129 are `Open-Ended
Registration`, the "Registered prior to RPP publication" form), the node is where the
report lives. Naming the registration in `alt_identifier_r` is true; putting it in
`url_r`, replacing the link to the report, is not.

## Candidate narrowings, none measured

1. **Stand down on a results-reporting construction in the record's own text** —
   past-tense "I/we replicate(d) … these results", a reported test statistic that is not
   in a power-analysis field. This is what would have saved `pr8a4`. It is a statistical
   claim and needs its own sample.
2. **Discard on an enumerated template list rather than a negation.** Twelve of the
   thirteen templates are named above; an enumerated discard is a `trusted` structural
   claim, and an unknown template then falls through to `pending` rather than to
   deletion. This does not fix `pr8a4` (EGAP is enumerable and would still discard it),
   so it is a complement, not an answer.
3. **Route to `needs_human` instead of `discard`.** The population is 1,308 rows, which
   is a readable number, and the pile costs nothing to leave sitting.

Not a candidate: trusting OSF's own timing fields (`q8`/`q10`, "Registration prior to /
following analysis of the data"). `pr8a4` fills its own field wrongly, and `6me7j` shows
the template name and the timing field disagreeing in the other direction.

## Reproducing this

```bash
python analysis/osf_registrations/build_worklist.py /tmp/osf_worklist.parquet
OSF_RATE_SEC=0.5 python -m filter.engine.backfill \
    --worklist /tmp/osf_worklist.parquet --overlay-dir /tmp/osf_overlay --source osf --run
python analysis/osf_registrations/census.py          # reads the overlay, writes census.csv
```

`OSF_TOKEN` is optional and only raises the api.osf.io throttle. The fetch is free and
took 59 minutes for 3,016 rows at `OSF_RATE_SEC=0.5`; every answer is cached per DOI in
`cache/abstracts/`, so a re-run costs nothing.
