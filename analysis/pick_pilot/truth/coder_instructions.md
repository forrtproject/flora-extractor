# Coder instructions — which ORIGINAL study does this paper re-test?

You are establishing ground truth for a set of published replication (or reproduction)
papers. For each paper you will be given one evidence packet. Decide, from the paper
itself, which earlier published study or studies it sets out to re-test. Work
independently: do not look at any other file in this repository besides your packets
and this file, and do not try to infer what any automated system answered.

## Your batch

Your batch file (`analysis/pick_pilot/truth/batch_NN.txt`) lists OpenAlex work ids,
one per line. For each id, read `analysis/pick_pilot/truth/packets/<work_id>.md` and
write `analysis/pick_pilot/truth/answers/<work_id>.json`. Write nothing else.

## What a packet contains

- The replication paper's title, authors, year, journal, DOI/URL and abstract.
- **The offered list**: the paper's reference list (and sometimes a short
  "works this paper cites, pre-matched on author-year" list), each entry with an
  `@key` such as `@smith2009`. The list is machine-built from bibliographic metadata:
  it can be incomplete, and it can contain entries that are not cited in the paper at
  all. The original is NOT guaranteed to be on it.
- The paper's full text, when we had it (17 of 150 packets). Most packets have none.

## How to find the original

1. Read the abstract. If it names the original unambiguously ("we replicate Smith &
   Jones, 2009, Study 2"), that may be enough — but check the full text or the paper
   itself whenever the abstract only says "a previous study", "earlier findings",
   "the original study", or names authors without a year.
2. If the packet has no full text, or the full text does not settle it, **look the
   paper up online** through its DOI (publisher page, PubMed / PMC, Europe PMC,
   Google Scholar, preprint servers, ResearchGate) and read the introduction and
   methods. Report what you actually used in `evidence_basis`.
3. Only then match what the paper names to the offered list.

## What counts as an original

- The original is the earlier published study whose finding this paper sets out to
  re-test — by collecting new data to check whether it holds, or by re-analysing that
  study's own data — as identified by the paper itself, usually in the introduction or
  methods ("we replicate X et al., 2009"; "the present study attempted to replicate
  the finding of ...").
- Judge the RELATIONSHIP, not the wording: a paper that tests whether a specific
  published result holds in a new sample is a replication whether or not it says
  "replication". A study cited for background, motivation, measures or context is NOT
  an original. Nor is a study that is merely topically similar, prominent, or the
  only plausible-looking entry on the list.
- **Self-replication**: when authors replicate their own earlier study (e.g. "Buying
  books online: a replication" by the authors of an earlier paper on the same topic),
  that earlier paper is the original.
- **Genetic association replications**: the originals are the earlier association
  reports whose variant–trait associations are re-tested. If the paper re-tests
  associations from many papers, list each one it names as a re-tested finding
  (typically in a table or the introduction); if that is impractical (> ~10), list
  the ones you can identify and explain in `notes`.
- **Replication chains**: when a finding has already been re-tested by others, the
  original is the study closest to this paper in the chain — the one it actually
  re-tests — not the chain's first source. If the paper explicitly re-tests both,
  list both.
- **Several originals**: list one entry per original PAPER (several studies of one
  paper = one entry; mention the study numbers in `citation`). A multi-site project
  re-running one protocol is one original.
- **Two reports of the same study** (e.g. a conference abstract and the journal
  article, or a preprint and its publication): list the one the paper cites; if it
  cites both, list both and say so in `notes`.
- **Psychometric "replications"** (re-validating a questionnaire's factor structure
  in a new sample): the original is the paper that reported the structure / developed
  the instrument being re-tested, when the paper frames it that way.

## Matching to the offered list

- If the original is on the offered list, give its `@key` exactly as written (with
  the `@`), and set `on_list: true`. Check year, authors AND title — an author-year
  match to the wrong paper by the same authors is the most common trap, so pick the
  entry whose title is the study the paper actually re-tests.
- If two list entries are genuinely the same work (duplicate records), give the one
  that fits best and mention the other key in `notes`.
- If the original is not on the list, set `key: null`, `on_list: false`, and give the
  full citation (authors, year, title, journal) and DOI if you can find it. This is
  the `not_on_list` case; it is an important answer, not a failure.
- Give `citation` and (when findable) `doi` for on-list originals too.

## When the paper re-tests nothing

Set `not_a_replication: true` and `originals: []` when the paper does not re-test any
specific earlier published study — e.g. it only calls itself a replication of "the
literature", reports a new study, is a review, or "replication" refers to biological
replicates / sample replication cohorts within the same paper. Explain in `notes`.

If you believe the paper does re-test a specific study but you cannot identify which
even after looking it up, set `originals: []`, `not_a_replication: false`,
`confidence: "low"` and explain in `notes`.

## Output — one JSON file per work

`analysis/pick_pilot/truth/answers/<work_id>.json`:

```json
{
  "work_id": "W2075456319",
  "originals": [
    {"key": "@yang2003", "citation": "Yang, B., & Lester, D. (2003). Buying books online: Follow-up. Perceptual and Motor Skills.", "doi": "10.xxxx/...", "on_list": true}
  ],
  "not_a_replication": false,
  "confidence": "high",
  "quote": "verbatim sentence(s) from the paper that identify the original",
  "evidence_basis": "packet",
  "notes": ""
}
```

Field rules:

- `originals`: list, in the order the paper presents them. `key` is the `@key` string
  or JSON `null`; `doi` is a bare DOI (`10.…`) or `""` when unknown; `on_list` is a
  boolean.
- `not_a_replication`: boolean.
- `confidence`: `high` (the paper names the original unambiguously and you matched it
  with certainty), `medium` (strong but indirect, or a close call between list
  entries), `low` (a guess, or you could not access enough of the paper).
- `quote`: a verbatim passage from the paper (abstract or full text, or the online
  version) that identifies the original. `""` only if you had no text to quote.
- `evidence_basis`: `packet` (abstract/metadata only), `fulltext` (the packet's full
  text), or `online` (you read the paper, or its abstract/introduction, online).
  Combine with `+` if relevant, e.g. `packet+online`.
- `notes`: anything a reviewer should know — ambiguity between keys, duplicate list
  entries, why `not_on_list`, originals you could not identify. Keep it short.

Write valid JSON (UTF-8). Do one file per work, even when the answer is uncertain.
