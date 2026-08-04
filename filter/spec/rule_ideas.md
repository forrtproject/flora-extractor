# Rule ideas: the v1 rule book, archived

What this is: a human-readable record of every spec the rule-book v2 rewrite deleted, and
of every idea that was considered and not shipped, so that recovering a pattern or a
rationale never needs git archaeology. The full JSON of every rule named here is in git
history at **`727b378`** (`git show 727b378:filter/spec/<file>.json`). The design that
replaced them, and every measurement it rests on, is `redesign/rulebook_v2.html`.

The v2 bundle is a **whitelist**: nothing is screened unless a positive rule admits it.
That single inversion is why most of what follows is deleted rather than rewritten — a
sense-disambiguation exclusion has nothing to do when the sense was never admitted.

---

## 1 · Deleted specs

### `biological` — discard, 545, live

`"<organism> replication"`: the organism term is the head of the phrase, so the sense is
fixed by the words. Existed because bare `replication of` and the bare replication stem
admitted virology and molecular biology in bulk. Named-pathogen terms (hiv, sars-cov-2,
influenza, `<x>virus`) were added on top of the original dna/rna/viral/virus list after
titles like "Suppression of HIV-1 Replication" got through.

```
\b(?:dna|rna|mrna|cdna|viral|\w*virus(?:es)?|hiv|sars[-\s]?cov[-\s]?2?|influenza|bacteri\w+|mycobacteri\w+|pathogens?|parasites?|prions?|phages?|cell|cellular|chromosome|plasmid|genome|genomic|mitochondrial)\s+replication\b
```

Measured: `trusted`, structural — no sample. In production in `keyword_verdict()` and in
Stage 1's scan since Stage 2 existed.

**Why v2 deletes it:** exclusions are unnecessary under a whitelist. "DNA replication" is
not discarded; it is simply never admitted, because it fires no admission arm. Its job
went nowhere, because there is no longer a job.

### `biological-of` — discard, 543, shadow

`"replication of <organism>"` word order, which `biological` does not cover. One
duplicated virology cluster phrased that way accounted for 106 of the 501 rows Stage 3
later screened out as `not_a_replication` — the measurement that motivated the pattern.
Up to three filler words are allowed before the organism; `cell`/`cellular`/`parasite`
must be the HEAD of the phrase (followed by punctuation, end, or a preposition/verb),
because with a three-word filler window the unrestricted form killed
"a direct replication of the classic **cell phone** driving study" and
"a conceptual replication of the **parasite stress** theory of values" outright. #147
(BO1) exempted `genome-wide` so that "Replication of Genome-Wide Association Studies of
Type 2 Diabetes Susceptibility in Japan" survives; the Unicode dash range matters because
several affected titles write "Genome‐Wide" with U+2010.

The RE2 decomposition that shipped (three arms):

```
\breplication of (?:the\s+)?(?:\w+[-\s]+){0,3}(?:dna|rna|mrna|cdna|viral|\w*virus(?:es)?|bacteri\w+|mycobacteri\w+|pathogens?|prions?|phages?|chromosomes?|plasmids?|hiv|sars[-\s]?cov[-\s]?2?|influenza|mitochondrial|organoids?)\b
```
```
\breplication of (?:the\s+)?(?:\w+[-\s]+){0,3}(?:genomes?|genomic)(?:$|[^-\s‐-―\w]|[-\s‐-―](?:$|[^w]|w(?:$|[^i]|i(?:$|[^d]|d(?:$|[^e])))))
```
```
\breplication of (?:the\s+)?(?:\w+[-\s]+){0,3}(?:cells?|cellular|parasites?)\b\s*(?:[.,;:)\]]|$|(?:in|by|during|within|with|and|or|is|are|was|were|from|to|at|under)\b)
```

The faithful original, which RE2 cannot run (`pyre_regex`, kept verbatim):

```
\breplication of (?:the\s+)?(?:(?!(?:study|studies|experiments?|effects?|findings?|results?|analys[ei]s|papers?|trials?|surveys?)\b)\w+[-\s]+){0,3}(?:(?:dna|rna|mrna|cdna|viral|\w*virus(?:es)?|bacteri\w+|mycobacteri\w+|pathogens?|prions?|phages?|chromosomes?|plasmids?|genomes?(?![-\s‐-―]?wide)|genomic(?![-\s‐-―]?wide)|hiv|sars[-\s]?cov[-\s]?2?|influenza|mitochondrial|organoids?)\b|(?:cells?|cellular|parasites?)\b(?=\s*(?:[.,;:)\]]|$|\b(?:in|by|during|within|with|and|or|is|are|was|were|from|to|at|under)\b)))
```

Measured: `downstream` n=501 (the 106-row virology cluster) for the motivation, plus a
`heuristic` #147 cost count over 5,604,314 snapshot rows — BO1 costs 0.4 extra rows per
million scanned, ~204 rows over the full 510M-row snapshot. Shadow throughout, because
the decomposition drops the study-noun negative lookahead and so widens the discard.

**Why v2 deletes it:** same as `biological` — the whitelist never admits the virology
sense, so nothing needs disambiguating. The pattern it protected against
(`replication of <organism>`) is the object of bare `replications? of`, which is now
rule C's last, explicitly-warned arm rather than an admission route.

### `structural` — discard, 544, live

Molecular-biology terms of art, enumerated rather than stemmed:

```
\breplication (?:fork|origin|stress|timing)\b
```

Measured: `trusted` — no social-science homograph exists for any of the four nouns.

**Why v2 deletes it:** exclusion unnecessary under a whitelist.

### `technical-object` — discard, 541, shadow

Storage/distributed-systems replication, both word orders. #147 narrowed the object list
by removing model/method/data/dataset, which is how a computational reproduction
describes itself ("A Replication and Analysis of Tiebout Competition Using an
Agent-Based Computational Model", "Replicating MOOC predictive models at scale"); the
wide pattern was killing those terminally.

```
\b(?:replication of (?:the )?(?:apparatus|code|database|pipeline|protocol|software|simulation)|(?:apparatus|code|database|pipeline|protocol|software|simulation)\s+replication)\b
```

Measured: `trusted` for the naming argument plus `heuristic` n=5,604,314 — the narrowing
costs 3.9 extra rows per million scanned (~1,990 over the snapshot). A matched-span
census over the same 5.6M rows found "data replication" 36 times, "database replication"
7 and "model replication" 3. What the rule *discards* was never read, which is why it
stayed shadow to the end.

### `technical-verb` — discard, 540, shadow

Verb form of the above, narrowed identically (#147, TV1_tight_objects):

```
\breplicat(?:e|ed|ing)\s+(?:the )?(?:apparatus|code|software|pipeline|script|database)\b
```

Measured: `trusted` for the naming argument plus `heuristic` n=5,604,314 — 3.0 extra rows
per million scanned (~1,531 over the snapshot). The one hand reading of rows the pattern
suppresses at the margin found **3 of 18 in scope**, which is the only direct signal about
its precision and far too small to license a live discard.

**Why v2 deletes both:** exclusions unnecessary under a whitelist. Note the flip side: the
CS storage sense they were built for is also not admitted by any v2 arm, so those rows now
land in `pending` rather than in `discard`.

### `data-availability` — discard, 550, shadow

Dataverse/OSF/GitHub boilerplate — "data and code are available on OSF to reproduce the
results in this paper" — which describes the paper's OWN reproducibility package, not a
reproduction of someone else's study. The availability anchor is REQUIRED; without it the
pattern claimed ordinary methods prose and made genuine reproductions terminal.

Shipped decomposition (both clauses ANDed row-wide):

```
\b(?:data|code|scripts?|files?|materials?|repositor\w+|replication package)\b[^.\n]{0,80}?\bto\s+(?:replicate|reproduce)\b
```
```
\b(?:data|code|scripts?|files?|materials?|repositor\w+|replication package)\b[^.\n]{0,200}\b(?:available|provided|deposited|archived|posted|shared|supplement\w*|repositor\w+|osf|github|dataverse|zenodo|figshare|doi\.org)\b
```

Faithful original (`pyre_regex`, same-sentence via lookahead):

```
\b(?:data|code|scripts?|files?|materials?|repositor\w+|replication package)\b(?=[^.\n]{0,200}\b(?:available|provided|deposited|archived|posted|shared|supplement\w*|repositor\w+|osf|github|dataverse|zenodo|figshare|doi\.org)\b)[^.\n]{0,80}?\bto\s+(?:replicate|reproduce)\b
```

Measured: `heuristic` only — the argument on offer was incumbency in `keyword_verdict()`,
which is not a structural claim, and no sample of its discards was ever read.

**Why v2 deletes it:** the boilerplate it names carries no v2 admission arm, so the rows
are never admitted in the first place. (`\bto replicate\b` alone is not an arm; the
fail/attempt family and the first-person family both require more.)

### `exclusion-rescue` — screen_cheap, 650, live (173 lines)

The #44 targeted readmission. Logic, in three ANDed clauses:

1. **an exclusion context fired** — a copy of the editorial-artifact anchor, the
   data-availability pair, `biological`, `structural`, all three `biological-of` arms,
   `technical-object` and `technical-verb`, kept hand-synced with the originals;
2. **AND a replication or reproduction phrase is present** — a copy of the phrase
   alternation (the pre-#147 list plus the reproduction arms);
3. **AND a specific author-year cite is present** — the RE2-safe cite regex below.

It outranked the whole 500s band so the readmission won, and landed in `screen_cheap`:
enough evidence to refuse a terminal discard, not enough to buy the expensive screen.

**Why v2 deletes it:** nothing to rescue from. The one job worth keeping — "a strong
admission should beat a fallible discard" — is now a precedence number: admission
(`replication-claim`, 700) sits *above* the metadata-crosswalk discard
(`not-a-study-type`, 500) and *below* the definitional discards (940–960).

### `title-phrase-rescue` — screen_cheap, 645, shadow (156 lines)

#147 item 2. An exclusion fired in the ABSTRACT ONLY while the TITLE itself states the
design — "A Replication of 'The Role of Intrafirm Networks'" whose abstract says "we
replicated the model". Expressed declaratively as three clauses: a title phrase (the
phrase list AS MEASURED, i.e. the pre-#147 alternation, written as `title_regex`), AND the
`technical-object`/`technical-verb` patterns matching `abstract_regex`, AND a `none_of`
where the same two patterns match `title_regex`. Scope was technical-object and
technical-verb only; the DA1 arm was withheld because `data-availability` was shadow, and
the `biological`/`biological-of` title rescues (BI1, BO3) were refused on cost (BI1 alone
moves ~2,093 rows across the snapshot, twenty times the whole TV3+TO4 rescue).

Measured: `downstream` n=5,604,314 — TO4 contributes 0 influx rows in 5.6M and TV3 one
(0.2 per million, ~102 rows projected over the full snapshot).

**Why v2 deletes it:** same as `exclusion-rescue`. Its target rules no longer exist.

### `phrase-with-cite` — screen_expensive, 350, live

The only route to the expensive two-voter screen in v1: a replication or reproduction
phrase AND an author-year citation, with a row-scoped `none_of` carrying the GWAS
vocabulary (a discovery/replication-cohort design says "we replicated one SNP" and means
something else). #147 (G1 minus its `replicated the association` alternative) made the
guard stand down when the row also names a prior report.

**The RE2-safe author-year cite regex — the most valuable single pattern in the deleted
book, and the reason this file exists.** It replaces
`extract_author_year_patterns()`'s lookahead-based patterns and its bare-name blacklist,
and covers parenthetical, narrative and et-al forms. The name atom is a negated character
class rather than `\w` because Python `re` reads `\w` as Unicode-aware and RE2 (pyarrow)
as ASCII, so an accented surname in cite position — García et al. (2020) — matched one
backend and not the other; the trailing `\b` became an explicit non-digit for the same
reason:

```
\(\s*[^ \t\n\r\f,;:()\[\]]{2,}(?:\s+et\s+al\.?|(?:\s*,\s*[^ \t\n\r\f,;:()\[\]]{2,}){0,3}\s*,?\s*(?:and|&)\s*[^ \t\n\r\f,;:()\[\]]{2,})?\s*,?\s*(?:19|20)\d{2}[a-z]?\s*[);,]|[^ \t\n\r\f,;:()\[\]]{3,}(?:\s+et\s+al\.?|\s*(?:and|&)\s*[^ \t\n\r\f,;:()\[\]]{3,})?\s*['’]?s?\s*\(\s*(?:19|20)\d{2}[a-z]?\s*\)|[^ \t\n\r\f,;:()\[\]]{3,}\s+et\s+al\.?,?\s+(?:19|20)\d{2}(?:[^0-9]|$)
```

The GWAS guard, also worth keeping (`none_of` of an `all_of`):

```
\b(?:gwas|genome[-\s]wide|snps?|alleles?|genotyp\w+|haplotype|linkage disequilibrium|minor allele frequency|loci|polymorphism\w*|replication cohort|discovery cohort|exome)\b
```
…AND `none_of`:
```
\b(?:previously|prior|earlier|original|published|reported\s+by)\b
```

Measured: two `downstream` entries over the 2,892,614-row snapshot corpus — G1− costs 0.0
extra admissions per million; the T2 phrase arms cost +9.3 per million (+0.6%).

**Why v2 deletes it:** §1.2 of the proposal. On the rows where bare `replication of` fires
and no STRONG arm does, the row-scoped cite is present in 61% of positives and **42% of
negatives** — it does not separate them. A cite is not an admission signal; it only orders
spend. The expensive route is now `replication-claim`, which asks the paper to say that
*it* is or did a replication, and asks for no cite at all. The GWAS guard is not carried
over: no v2 arm admits "we replicated the association" without a first-person or
qualifier construction, and the guard's evidence was gathered against an admission rule
that no longer exists.

### `phrase-replication` — screen_cheap, 260, live

The 33-arm replication-phrase alternation. Twelve of its arms survive, restated, in
`replication-claim`; one (`\breplications? of\b`) becomes `replication-signal`'s last arm.
The rest are §2 below.

Measured: `downstream` n=2,892,614 — the #147 T2 additions cost +9.3 admissions per
million (+0.6%).

### `title-stem` — screen_cheap, 240, live

**Carried into `replication-signal` arm 1 verbatim, not deleted.** The 15 multilingual
replication stems, TITLE ONLY, because a title is a handful of topical words while the
same stem in a 1,500-character abstract is noise. 111 of 112 Korean 재검증 works are
invisible to the English stems, and this arm alone rescues 3.4% of known positives that no
phrase arm reaches.

```
(?i)replicat|replicab|reproduc|reanalys|re-analys|reanalyz|re-analyz|replikat|réplicat|replicaci|replicaç|replicazion|reproduç|reproduzi|追試|반복검증|재검증
```

### `nfd-stems` — screen_cheap, 205, shadow

The same stem list written with combining diacritics, because `title_regex` spells its
accented stems in composed (NFC) form while OpenAlex titles are not normalised, so a title
carrying "re" + U+0301 + "plicat" matched nothing.

The pattern, written as escapes because the combining marks are invisible in most
editors — this is exactly what the JSON held (`"(?i)réplicat|replicaç|reproduc"`):

```
(?i)re<U+0301>plicat|replicac<U+0327>|reproduc
```

i.e. `re` + COMBINING ACUTE ACCENT + `plicat`, `replicac` + COMBINING CEDILLA, and the
plain `reproduc` stem carried along for symmetry.

**Why v2 deletes it:** see §5 — NFC normalisation at the matching seam makes a second stem
rule unnecessary.

### `concept-replication` — screen_cheap, 220, live

**Carried into `replication-signal` arm 3.** OpenAlex concepts C12590798 (Replication
(statistics)) and C9893847 (Reproducibility) — the Stage 1 harvest arm. Divergence 1 in
`docs/filter-engine.md`: v1 Stage 2 wrote a concept-only row `false_positive` terminally;
the engine routes it to the cheap discard-only tier instead, because a concept is a
topical signal and a rule that cannot admit must not be allowed to kill.

### `reproduce-verb-arms` — screen_cheap, 210, shadow

Bare `reproduce/reproduces/reproduced/reproducing` verb uses that no anchored pattern in
`phrase-reproduction` (now `reproduction-signal`) claims:

```
\breproduce[sd]?\b
\breproducing\b
```

**Why v2 deletes it:** parked for issue #155 — see §3.

### `dataset-type` — discard, 950, shadow

**Absorbed into `no-codable-text`.** OpenAlex `type == dataset`, kept as its own rule
because it was the #149 decision and carried a standing audit obligation. Evidence from
the 2026-08-03 pilot: dataset rows are 15–33% of every admission arm, and in the concept
arm 69.8% of them proceed past the screen against 12.5% for articles — not because they
are replications but because the screen cannot discard a row whose text it cannot read.
The measurement is a screen-spend figure, not a precision figure, which is why the rule
was `heuristic` and shadow. v2 keeps the discard but changes the claim it rests on: not
"this is not a replication" but "this object has no codable text", which is settled by
inspection rather than by a precision sample.

### `deposit-doi-prefixes` — discard, 960, live

**Absorbed into `not-a-paper-doi` arm 3, minus figshare.** Ten confirmed deposit-only
registrants, each separately confirmed against DataCite:

```
10.11587  10.15139  10.18170  10.18710  10.18738  10.21979  10.2905  10.34894  10.3886  10.7910
```

figshare was an eleventh, expressed as `^10\.6084/` (a regex rather than a prefix entry
only because `non_article_doi()` matched it on the "10.6084/" boundary). **Dropped in v2
per decision D1**: figshare is a general-purpose registrant hosting preprints and
technical reports, not a deposit-only one, and an implementation detail was leaking into
policy. Zenodo (10.5281) is deliberately absent for the same reason — CODECHECK
certificates live there.

The arm rests on a claim that has never been measured: "the paper is in the corpus under
its own DOI, so discarding the deposit loses nothing." That is a joint probability (parent
DOI resolvable × parent in pool × parent admitted by another route), not a structural
fact. V1's parent-recovery measurement (proposal §7) is owed; #157 holds the deferred
linkage-worklist alternative.

### `non-article-doi` — discard, 955, live

**Absorbed into `not-a-paper-doi` arm 1**, verbatim, with its `trusted` rationale:

```
/reviews/|/decisions/
```

A publisher-minted path segment for the review object (e.g.
`10.7287/peerj.10325v0.1/reviews/2`). These records echo the reviewed paper's title
verbatim, so every replication phrase in the title fires on something that is not the
paper. The proposal recommends anchoring to known registrants rather than matching
anywhere in the DOI; not done in v2, recorded here as an open narrowing.

### `editorial-artifact` — discard, 555, live

**Absorbed into `not-a-paper-title`**, pattern verbatim, precedence raised 555 → 955:

```
^\s*(?:review for|decision letter for|peer review #|faculty opinions recommendation of|correction to|author response)\b
```

Every alternative names a document genre followed by its parent, and the pattern is
start-anchored, so it fires on titles that announce what the object is and not on papers
that discuss the genre.

### `non-article-type` — discard, 945, live

**Split across `no-codable-text` (940) and `not-a-study-type` (500)** by one criterion:
would the object have text if it were a study?

| type | v2 home |
| --- | --- |
| `component`, `database`, `software`, `supplementary-materials` | `no-codable-text` (940) |
| `grant`, `libguides`, `paratext`, `peer-review`, `standard` | `not-a-study-type` (500) |

`dataset` joins the first group from `dataset-type`. Nothing was dropped. `letter` was
never in the list and stays out (maintainer ruling: journals publish replication reports
as letters, and a letter has an abstract). `other` (the registries' catch-all),
`data-paper` and `software-paper` (papers ABOUT data) are deliberately absent, as are
`editorial` and `erratum` — adding them would be a decision, not a cleanup.

The v1 motivation was a hand-check of 50 provisionally-linked rows that found 23
non-studies (Dataverse/Zenodo/Mendeley deposits, eLife "Author response" objects), which
measured the RECALL of the type signal among known non-studies, not the precision of the
discard.

---

## 2 · `phrase-replication` arms NOT carried into `replication-claim`

Per the §1.1 greedy set cover, the thirty arms below the top twelve buy about **1% of
recall** (28 papers out of 2,895) and carry the whole hand-sync maintenance cost. They are
recorded here verbatim for possible Phase-3 re-adoption, one family at a time and each
measured marginally.

Subsumed by a surviving arm (no recall lost, listed for completeness):

```
\bwe replicated\b                       → covered by \bwe\s+(?:\w+\s+){0,2}replicat(?:e|es|ed)\b
\bwe replicate\b                        → same
\bwe\s+\w+ly\s+replicate[sd]?\b         → same
\bdirect replications?\b                → covered by the qualifier family arm
\bconceptual replications?\b            → same
\bregistered replications?\b            → same
\bexact\s+replications?\b               → same
\b(?:close|high[-\s]powered|pre[-\s]?registered|large[-\s]scale)\s+replications?\b  → same
\b(?:many-?labs?|multi-?site)\s+replications?\b                                     → same
\bfailed to replicate\b                 → covered by the fail/attempt arm
\battempt\w*\s+to\s+replicate\b         → same
\b(?:unable|inabilit\w+|failure)\s+to\s+replicate\b → fail/attempt arm + the failure-noun arm
\bdid not replicate\b                   → covered by the negation matrix
\baim\w*\s+to\s+replicate\b             → covered by the aim/set-out arm
\bset\s+out\s+to\s+replicate\b          → same
```

Genuinely dropped, with their patterns:

```
\bregistered report of\b
```
Deliberate exclusion, not an oversight: most Registered Reports are original studies.

```
\bwe\s+(?:conducted|performed|carried\s+out)\s+a\s+replication\b
```
A three-verb closed list where `we (…0-2 words…) replicat(e|es|ed)` already covers the
verb constructions; the noun-object form it adds is rare enough not to appear in the set
cover's top twelve.

```
\bcross-?(?:cultural|national|lab(?:oratory)?)\s+replications?\b
```
Cross-cultural/national/lab replication. A real genre; candidate for Phase 3 as its own
arm with its own marginal count.

```
\b(?:sought|seek(?:s|ing)?|tri(?:ed|es)|try(?:ing)?|wanted|intend(?:ed|s|ing)?|hoped?|started|planned|undertook|proceeded|decided|chose)\s+to\s+(?:\w+ly\s+)?replicate\b
```
The matrix-verb family (#147 C1b, 63 of the 319 no-phrase gold misses). Note the arm
deliberately omits `able to replicate`, whose cost was the entire difference between C1
and C1b: "a prosthetic foot able to replicate the function of the biological foot".
Strong Phase-3 candidate — it is a distinct construction, and several of its verbs
(sought, tried, undertook) are unambiguously about the current paper.

```
\breplicat(?:e|es|ed|ing)\s+(?:the\s+)?(?:previous|prior|original|earlier|main|key|core|published)?\s*(?:findings?|results?|effects?|analys[ei]s|stud(?:y|ies))\b
```
`replicat* the findings/results/effects/analysis/study`. The optional modifier makes the
bare "replicate the results" form reachable, which is where its noise lives.

```
\battempted\s+replications?\b
```
Third-party framing ("three attempted replications of…"), which the fail/attempt verb arm
does not reach.

---

## 3 · Reproduction material, parked for issue #155

Reproduction is a different genre with a different precision profile and needs its own
evidence. The v1 rule `phrase-reproduction` (ten anchored patterns, precedence 262) is
carried into the v2 bundle **verbatim and live**, renamed `reproduction-signal` to match
the bundle's claim · signal · probe naming, so the reproduction stream does not go dark.
Parked alongside it:

- **`reproduce-verb-arms`** — the bare-verb arms above (`\breproduce[sd]?\b`,
  `\breproducing\b`). The open question is how much of the computational-reproduction
  genre the anchoring costs, and it can only be answered by counting what the arm moves.
- **Reproduction-side probe ideas** (proposal §3 D small print, deliberately NOT in
  `replication-probe`): `re-estimat*`, `reimplement*`, `rerun`, CODECHECK certificates,
  artifact-evaluation reports.
- **The measured negatives that shaped the anchoring**: bare `reproduction of` is animal
  breeding, social reproduction and epidemiological R₀ — 8% precision over 15,440 rows, 0
  of 15 sampled hits in our sense (#137). `re-analysis of` is dominated by secondary
  analyses asking new questions of old data. Generic `comput* reproduc*` is workflow
  engineering, pedagogy and policy.
- **Why nothing here is settled by the v2 measurements**: the positives corpus contains
  too few reproductions to say anything — `comput* reproduc*` fires on 4 of 2,895,
  anchored `reproduction of the … results` on 3.
- **D6, still owed**: CS reimplementation and benchmarking — "we reimplemented and could
  not reproduce the reported accuracy" — in scope or not, is a question for #155.

---

## 4 · Codex's title-vs-abstract arm split (Phase 3)

Not implemented in v2; recorded because it is the sharpest available refinement of rule B
and it costs nothing to keep.

The arms that report *someone else's* result — `replication study`, `failed to
replicate`, `direct replication` and the many-labs family — are strong **in a title** but
can be literature-review language **in an abstract**. The refinement splits each into:

- a `title_regex` arm at full strength, and
- a `text_regex` arm additionally requiring a current-paper cue (`this|the present|our|we`)
  nearby.

That is a narrowing inside one rule rather than a new rule, which is exactly the property
the whitelist inversion buys. It should be applied after Phase 1's downstream precision
estimate says which arms actually need it — measuring first is the point.

---

## 5 · NFC normalisation (the `nfd-stems` replacement)

`nfd-stems` existed only because decomposed-Unicode titles miss the composed multilingual
stems. v2's answer is to normalise once at the seam where text is materialised for
matching, so every rule gets it free and no rule needs a decomposed twin.

**Outcome: the seam was clean and the change shipped.** `filter/engine/backends.py` has
exactly one place per backend where title and abstract become matchable text:

- row backend — `_row_title()` / `_row_abstract()`, which `_row_text()` and
  `match_evidence()` both read;
- batch backend — `BatchContext.__init__`, which derives `title`, `abstract`, `text` and
  `abstract_empty` once per batch.

Both now apply NFC. **Not with `pc.utf8_normalize`:** on pyarrow 25.0.0 that kernel
returns its input unchanged for `form="NFC"` (verified — a title spelled `R e U+0301 p …`
comes back spelled `R e U+0301 p …` for both NFC and NFD), so using it would have parted
the two backends on exactly the rows the change exists for. The batch path instead makes
one Python pass per batch (`_nfc_array()`), guarded by `unicodedata.is_normalized` so an
already-composed batch is returned untouched; the cost is ~20 ms per 50,000 rows against
the ~2 s the batch already spends. DOIs are not normalised — they are ASCII by
construction and `clean_doi()` owns their canonical form. `tests/test_spec_vocabulary.py` pins that a
decomposed-Unicode title matches `replication-signal`'s multilingual stem arm.

Not done, and worth knowing: Stage 1's pool build (`search/snapshot_scan.py`) still writes
whatever OpenAlex gave it, so the pool itself holds mixed normal forms. Normalising at the
matching seam rather than at pool-build time was chosen because it fixes every consumer of
the engine without a pool rebuild; anything outside the engine that regex-matches pool
text still sees NFD.

---

## 6 · Anchoring experiments that FAILED for bare `replication of`

Recorded so nobody re-runs them. Measured on the rows where `replication of` fires and no
STRONG arm does (proposal §1.2):

| anchor on the object of "replication of" | positives gained | negatives admitted |
| --- | --- | --- |
| study-noun (`study / experiment / result / finding / effect / analysis / paper`) | +3.6% | +14.2% |
| proper name + year (`… of Smith et al. (2009)`) | +2.3% | +3.0% |
| quoted title (`… of "Title"`) | +0.2% | +0.2% |
| any of the three | +5.8% | +17.2% |

The best anchor (proper name + year) is roughly break-even; the union admits three
negatives per positive. `phrase-with-cite`'s row-scoped author-year cite does no better —
present in 61% of positives and 42% of negatives on the same rows. **No cheap syntactic
anchor makes bare `replication of` precise.** It belongs in the cheap tier or in shadow,
sized against the pool before it is switched on, and it must not be a route to the
expensive screen.

For context on why it cannot simply be dropped either: it is the highest-recall arm in the
whole book (40.4% of positives, 342 of them uniquely) and simultaneously fires on **82.2%
of screened negatives**.

---

## 7 · Open items the v2 bundle inherits

- **D2** — `correction to` and `author response` objects sometimes contain a substantive
  failed reproduction. Both are in `not-a-paper-title`'s live pattern. In scope as
  replication evidence, or out of scope as document types? A definition question for
  FLoRA, not a fact about the rule.
- **D3** — is a planned-but-not-completed replication a FLoRA record? Decides whether the
  `aim|set out to replicate` arm stays in B, drops to C, or gets its own pile.
- **D4** — `component` and `database` sit in `no-codable-text` on the same argument as
  `dataset`, and are the two worth a second look: a `component` can occasionally be an
  independently meaningful report.
- **D5** — the no-abstract cascade (proposal §5): narrow title rule → `fetch_abstracts.py`
  backfill → `needs_human` only where backfill failed. Not built; the engine already emits
  `pending/no_text` and the waterfall already exists, but they are not wired together.
- **#157** — deposit→parent linkage worklist, deferred; blocked on V1's parent-recovery
  measurement, which sizes the loss for `not-a-paper-doi`'s deposit arm and
  `no-codable-text` together.
- **The "one alternation" claim is not true until Stage 1 changes too.**
  `filter/phrase_detection.py` still defines the search vocabulary independently of this
  bundle. Stage 2 now has exactly one copy of the phrase list; Stage 1 has another.
