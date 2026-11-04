# Repository routes for held rows without a document (issue #188, routes 2 and 4)

Probed 2026-11-04 against live servers. **Scraping the landing page a
`hdl.handle.net` URL resolves to returned a document of the right paper for 8 of 40
sampled rows (20%, 95% interval 10–35%).** OAI-PMH reached 6 of 40, all among those 8.
Legacy DSpace REST (5%), the EPrints landing pattern (12%) and the Figshare API (5%)
miss the 15% bar. Only the handle landing scrape was implemented, as
`pdf_source = handle_landing`.

## Population and sample

Rows from `data/extracted.csv` and `data/target_pending.csv` with a blank `pdf_source`
and either `outcome = cannot_be_determined` or a `target_pending` row: 5,089 rows.

| Group | Rows | Distinct `url_r` | Sampled |
| ----- | ---: | ---------------: | ------: |
| `hdl.handle.net` | 206 | 202 | 36 |
| RePEc (EconPapers, IDEAS) | 21 | 21 | 4 |
| EPrints | 27 | 26 | 26 |
| Figshare | 42 | 42 | 40 |

- The sample uses seed 188 over distinct URLs, with handle and RePEc allocated
  proportionally within one group of 40.
- EPrints means a host containing `eprints` or a path containing `/id/eprint/`. That
  is wider than the 19 rows counted on #188 by host name alone, because it adds UCL
  Discovery, Greenwich, Pittsburgh and Concordia.
- One malformed URL (`hdl.handle.net10539`) is left out, so the handle and RePEc group
  is 227 rows rather than the 228 on #188.

## Yield per route

A document counts when it passes the module's format and title checks and its opening
page, read by hand, is the paper itself rather than a supplement.

| Route | Sample | Right paper | Yield | Unanswered or refused |
| ----- | -----: | ----------: | ----: | --------------------- |
| Handle landing page scrape | 40 | 8 | **20.0%** | 13 challenged, 3 outage |
| OAI-PMH (`didl`) | 40 | 6 | **15.0%** | 3 challenged, 1 outage |
| DSpace 7 `/server/api` (not on #188, exploratory) | 40 | 5 | 12.5% | 19 challenged |
| Legacy DSpace REST | 40 | 2 | 5.0% | 13 challenged |
| EPrints landing pattern | 26 | 3 | 11.5% | 2 challenged, 10 outage |
| Figshare API | 40 | 2 | 5.0% | none |
| Any handle or RePEc route | 40 | 10 | 25.0% | |

- **OAI-PMH adds nothing to the scrape.** Its 6 documents are all among the scrape's 8.
  Legacy REST's 2 are both EconStor and also among the 8. Only the DSpace 7 API adds
  documents, 2 from Helsinki, whose page renders its links client-side.
- **RePEc returned 0 of 4.** EconPapers pages link to the publisher or series, and no
  file passed the checks.
- **EPrints is 3 of 16 among rows whose host answered**, all three from UCL. Ten rows
  on six hosts (Kingston, UWE, Lancaster, Concordia, IOE, Trento) failed on a re-probe,
  and all six hosts also failed from the box, so they count as no document.
- **Figshare passes the title check 6 times but has 2 papers.** The other 4 are
  supplements, which carry the paper's title in their opening lines, so title coverage
  cannot separate them:
  - two ACS `_si_` files;
  - a Taylor & Francis `_sm` file;
  - a PLOS `.s003` file.

  Of the 40 records, 27 are figures and 2 are datasets. The Figshare website challenged
  all 40 requests, while the API and its download host answered.

## What handle URLs resolve to

From the 36 sampled handles. Some platforms behind a refusal are inferred from the host
or the OAI formats on offer.

| Platform | Rows | Note |
| -------- | ---: | ---- |
| DSpace 7 or later | 12 | Georgia Tech, ECU, ANU, Helsinki, IU Indianapolis, CONICET, Edinburgh, Maryland, Arizona |
| DSpace 6 or DSpace-CRIS 5 | 4 | EconStor, CSIC |
| DSpace, version unknown | 1 | Seoul National |
| IRIS (Cineca) | 6 | all answered 403; inferred |
| Behind a challenge, unidentified | 4 | NTU, Michigan, Murcia, Texas Tech |
| MPG.PuRe | 3 | all answered 403 |
| Primo (Leiden) | 2 | no links in the served page |
| NVA (Sikt, Norway) | 2 | no links in the served page |
| VuFind (ULB) | 1 | its only PDF link is the site's help file, refused by the title check |
| Handle that answers 500 | 1 | |

## How much of this is new

The retry logs in the box's cache show what the waterfall did for the 8 winning rows:

- **4 rows ran it and failed**, including `row_url`, `landing` and `playwright`:
  Georgia Tech, both CSIC rows and IU Indianapolis.
- **4 rows have no retry log and no document:** both EconStor rows, ECU and Maryland.
  Why is not established, and they may not have reached the waterfall. The extract dry
  run's offered count settles how many a re-run reaches.

So 4 of the 8 are shown to be documents the current waterfall misses. Across the
sample, 27 of the 36 handle rows have a retry log. The same landing pages also answered
the pipeline's own identity: all 8 winning pages and files returned a PDF under the
declared client, and the production link patterns found each one.

## Files

- `results.csv`: one line per sampled URL, with each route's outcome.
- `handle_works.txt`: the 204 work ids behind the 206 handle rows, for the re-run.
- `sample.py` and `probe.py`: the sample and the probe. The probe uses a scratch cache
  and the client name `flora-repository-probe/1.0`, at one request per second per host.
