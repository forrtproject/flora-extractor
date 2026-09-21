# Where FLoRA's scope and the Metascience Observatory's part company

The screen was asked about 573 Observatory replications our survivor pool never held
(`screen_offline.py`, shipped prompt and shipped voter pair). It said **proceed on 308
and discard on 265**. The 265 are the interesting half: works another curated database
calls replications and ours rejects.

**Read the 46% discard rate as a rate over this population only.** Every one of these
573 is a work the Stage 1 search gate never admitted, and the gate reads a replication
stem in the title or the abstract. So the population is, by construction, papers that do
not announce themselves — not a random sample of anything, and emphatically not FLoRA's
general false-positive rate.

## The 265 are two different stories, and only one is about scope

The causes file splits the population by what the gate could see (`not_in_pool_report.md`):

| | works | discard | proceed |
| --- | ---: | ---: | ---: |
| **F** — OpenAlex held **no abstract**; the gate saw the title alone | 354 | 110 | 244 |
| **G** — OpenAlex held an abstract, and it carries **no replication stem** | 219 | 155 | 64 |

Whether the abstract we later recovered from Europe PMC contains replication vocabulary
separates them completely:

| | carries `replicat*` / `reproduc*` / `re-analy*` |
| --- | ---: |
| F, proceed (244) | **147 (60%)** |
| F, discard (110) | 16 (15%) |
| G, proceed (64) | 0 |
| G, discard (155) | 0 |

### F is two things, and only its proceeds are the recall story

147 of these works **say in their own abstracts that they are replications** — "we
replicate and extend previous work", "We sought to replicate prior work showing that
older adults' reduced frequency of mind wandering…" — and our gate never saw the
sentence, because OpenAlex has no abstract for the record and the gate reads OpenAlex.
Europe PMC has the text: it supplied 303 of the 354 missing abstracts.

These are papers FLoRA wants, that FLoRA's own definition admits, lost to an
infrastructure gap rather than a judgement. Nothing about scope needs to change to want
them; the gate simply never read the page.

**F's 110 discards are NOT part of that story.** The F/G split is about what the GATE
could see, and the screen read a recovered abstract either way — so an F discard is a
judgement made on the same evidence as a G discard, and counts as a scope disagreement
exactly like one. Only 16 of the 110 carry replication vocabulary at all. The honest
statement is therefore: **all 265 discards are scope disagreements**; F and G differ
only in whether the work was ALSO invisible to Stage 1. The split matters for deciding
what to fix, not for counting the disagreement.

### G is the scope difference, and it is sharp

Of the 219 works whose abstract the gate DID read and found no replication word in,
**155 (71%) are discarded**. The two voters' reasoning is remarkably uniform — over all
265 discards, 62% say some form of *no stated aim to re-test a specific earlier
finding*, and 38% say the paper presents *original / new research*:

> "The paper presents a new theoretical model and eight original studies that test it,
> with no stated aim to re-test a specific earlier reported finding."
> — `10.1037/a0015850`, MO: close extension, success

> "The study is an original genetic association analysis, not a replication or
> reproduction of a specific earlier reported finding."
> — `10.1002/humu.20209`, MO: close extension, success

> "The abstract provides no indication that the paper aims to check a specific reported
> finding from earlier research."
> — `10.1111/ecoj.12042` (*Drought and Civil War in Sub-Saharan Africa*), MO: direct or
> close, failure

**The difference is what makes a study a replication.** FLoRA asks what the paper says
it is doing: a replication is a study that states an aim to check a specific earlier
finding. The Observatory asks what the study *does*: any design that re-tests a
previously reported effect counts, whether or not the authors frame it that way — a
judgement a curator or an LLM makes about the paper, not a claim the paper makes about
itself.

Neither is wrong. They answer different questions, and the gap is entirely predictable
from the definitions.

### Where the gap is widest

Within group G, by the Observatory's own labels:

| MO `replication_type` | discarded | of | rate |
| --- | ---: | ---: | ---: |
| conceptual | 22 | 25 | **88%** |
| close experiment | 25 | 29 | 86% |
| close extension | 64 | 83 | 77% |
| direct | 11 | 17 | 65% |
| direct or close | 32 | 63 | 51% |

| MO `discipline` | discarded | of | rate |
| --- | ---: | ---: | ---: |
| neuroscience | 18 | 19 | 95% |
| psychology | 80 | 98 | 82% |
| medical fields | 24 | 37 | 65% |
| economics | 18 | 39 | 46% |
| political science | 3 | 7 | 43% |

The ordering is the definitional one: the further a study is from re-running a named
earlier design, the less likely its authors are to call it a replication, and the more
the two databases disagree. `conceptual` at 88% and `direct` at 65% is that gradient.

Two disciplinary patterns fall out of it:

* **Genetic association studies.** The genetics literature treats each new cohort as a
  replication sample as a matter of routine, and writes it up as an original association
  study — "Scanning of genetic effects of alcohol metabolism genes…", "Excess maternal
  transmission of variants in the THADA gene…". The Observatory counts them; our screen
  reads an original study, because that is what the abstract describes.
* **Economics disagrees least** (46%), which fits: the economics replication genre is
  written as comments, revisitations and re-estimations, and those announce themselves.

One more asymmetry worth naming: across all 573, works the Observatory records as
`success` are discarded at 52% against 35% for `failure`. A study that confirms an
earlier finding while presenting itself as new work looks exactly like ordinary
research; one that contradicts a published result usually has to say which result.

## What follows

**For scope:** nothing here shows FLoRA mis-classifying. It shows FLoRA applying a
narrower, self-declaration definition consistently, and it quantifies the cost of that
choice against a database built on a wider one — 265 works in this sample, of which 155
were visible to Stage 1 and 110 were not.
Whether FLoRA wants the functional-replication class is a definitional decision for the
project, not something the screen can settle. If it ever does, this list is the ready
made evaluation set.

**For recall, there is something to fix now**, and it is independent of the scope
question: 147 self-declared replications were invisible because OpenAlex lacked their
abstracts and the gate had nothing else to read. Europe PMC held 86% of the missing
text. That is a Stage 1 question — whether the gate should read a second abstract source
before deciding — and it generalises past this list to every work in the snapshot with
no abstract.

---

## Where we both produce an answer, how often do we agree?

The 229 proceeds were run through Stage 3's own ladder (`extract_offline.py` →
`_process_row()`), which resolved 225 of 276 rows. Against the Observatory's own
columns:

**The original.** 164 of the 215 rows where both name a DOI point at the same original
— **76%**. We find nothing on 54 rows; the Observatory gives no DOI on 7.

**The outcome**, with ours mapped onto their vocabulary (`successful`→success,
`failed`→failure, `mixed` and `statistically successful but flawed`→inconclusive):

| | MO success | MO failure | MO inconclusive | MO reversal |
| --- | ---: | ---: | ---: | ---: |
| ours success | **90** | 2 | 18 | 0 |
| ours failure | 2 | **48** | 10 | 1 |
| ours inconclusive | 6 | 0 | **8** | 0 |

**79% agreement (146/185), and only 4 rows (2%) flatly contradict** — one database
saying success where the other says failure. The rest of the gap is decisiveness, not
direction: 28 rows where we commit and they record `inconclusive`, 6 the other way.

Read the four contradictions before trusting that 2%:

* `10.1016/j.drugalcdep.2018.03.013` and `10.1177/0163278712448772` — **we linked a
  different original**, so the two databases are not contradicting each other about the
  same claim. Both rows carry `link_confidence: low`, which is the pipeline saying so.
* `10.1001/archneurol.2010.350` — same original, genuine disagreement. Our quote ("The
  association of rs1990622 with AAO explains, in part, the wide range in the AAO…")
  reads as support; theirs says failure. A human should settle it.
* `10.3758/bf03193943` — *"Transient covert attention does alter appearance: A reply to
  Schneider (2006)"*. We coded `failed` from a quote that actually defends the original
  effect. **This one looks like our error**, and it is the genre that invites it: a
  reply defending a finding against a critique is not a replication reporting a failure.

So the honest reading of 2% is: one probable error of ours, one real disagreement, and
two cases of linking different originals. The rate is not a validation of either
database — the sample is 185 rows, and nobody has hand-checked them.

**Caveat on the join.** The Observatory carries one row per replication–original pair,
so a work with several originals has several results; the outcome comparison takes its
first row carrying a result. Where we resolved a different original than that row names,
the comparison is between answers about different claims.
