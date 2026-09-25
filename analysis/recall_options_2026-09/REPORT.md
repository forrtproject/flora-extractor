# Step 7 admission options — entries, cost, recall, precision (2026-09-25)

Offline against release c048ab6483d3 (bundle `b1e3a238`); nothing routed or written. Scripts:
`pool_pass.py`, `route_pubmed_gap.py`, `options.py`, `screen_sample.py`, `summarise.py`;
outputs `out/options_table.csv`, `out/proceed_rates.csv`, `out/handcheck_{B,C,R,PM}.txt`.

## Options
- **A** do nothing (title rules + `curated-observatory`): pile 9,106.
- **B** `replication-claim-general` (6 arms: qualifier+replication; "our replication";
  "replication attempt"; title subtitle ": A replication…"; title "replication of the/a…";
  "we … replicated"/"replication study" only with an original/previous/prior-study mention;
  molecular senses excluded in 4–6).
- **C** `replication-claim-text` live (8 broad arms in title or abstract).
- **D** C + `replication-claim-residual` (fail/attempt/aim to replicate, success* replicat*,
  negation matrix).
- Variants: `_SHL` (Social/Health/Life by level-0 concept, a proxy), `B−repo` (minus
  Zenodo/Dataverse/Figshare DOIs and "Replication package/data/files" titles), `C+B`.

## Table ("new" = beyond today's pile; "known" = FLoRA/Observatory/extracted DOIs)

| Option | New works | Screen $ | Proceed (95% CI) | Exp. proceeds | Extract $ | Known /1k new | Curated-only reached (/1,345) | FLoRA newly admitted (/1,058) |
|---|--:|--:|---|--:|--:|--:|--:|--:|
| A | 0 | 0 | — | 0 | 0 | — | 0 | 0 |
| **B** | 21,622 | 35 | 61.7% (53–70), n=120 | 13.3k | 9–19 | 29.9 | 533 | 543 |
| B−repo | 19,369 | 31 | 64% (67/104) | ~12.4k | 9–17 | ~32.9 | ≈533 | ≈540 |
| B_SHL | 14,507 | 23 | 68% (57–78), n=76 | 9.9k | 7–14 | 39.1 | — | — |
| C | 45,845 | 74 | 57.5% (49–66), n=120 | 26.4k | 18–37 | 16.3 | 861 | 621 |
| C_SHL | 35,010 | 56 | 58% (48–68), n=88 | 20.3k | 14–28 | 18.8 | 769 | 554 |
| C+B | 54,275 | 87 | 57% (50–65) | 31.2k | 22–44 | 16.7 | — | 754 |
| D | 76,380 | 123 | 44.5% (38–51), n=240 | 34.0k | 23–48 | 10.7 | 991 | 680 |
| D_SHL | 56,230 | 91 | 49% (42–56) | 27.5k | 19–39 | 12.8 | — | 605 |

Screen $ at the planning rate $161/100k (measured tokens suggest $40–100/100k); extract $ =
proceeds × $69–140/100k. OpenAlex ≈ 7 credits per extracted row. Money is not the binding
constraint (D < ~$175); the validation queue is (~3k rows in extracted.csv today).

Residual increment over C: 30,535 works at 3.6/3.5/3.5/0.7 known per 1k; proceed 25% (30/120).

## Hand-check of 20 random proceeds per sample (one reader)

| Sample | Clear | Borderline | No | The "no"s |
|---|--:|--:|--:|---|
| B | 9 | 3 | 8 | Zenodo "Replication Package" deposits, supplementary records, CS/ML |
| C | 8 | 8 | 4 | borderlines: replicate-and-extend, "we replicate prior research" |
| Residual increment | 7 | 6 | 7 | scale-validation ("did not replicate the factor structure") |
| PubMed admits (17) | ~4 | ~5 | ~8 | GWAS, clinical biomedicine |

Rough genuine yield (proceeds × clear share, wide error): B ≈ 6k, C ≈ 10k, residual ≈ 2.5k.

## PubMed supplement (~300k rows)
Admits: B 0.35% (~1.1k), C 1.5% (~4.5k), D 3.0% (~9.1k); proceed 50% (34–66%), mostly
biomedical. Recall-gap 165: rules alone reach B 57, C 124, D 133 — but with
`curated-observatory` live (as today) 164–165 are admitted under every option once in the pool.

## Separate finding
Today's pile admits 645 of the 1,703 FLoRA works in the pool (994 pending — 92 with no
abstract — and 63 match no spec). Stage 3 skips FLoRA works anyway; this is rule recall only.

## Recommendation
1. Promote **B−repo** (optionally `_SHL` if validation capacity is tight: 88% of B's known works
   on 67% of the volume). Keep `curated-observatory` live. ≈$50–60; ~12–13k proceeds.
2. Add **C_SHL** later as validation capacity allows.
3. Don't promote `-residual`.
4. Defer the PubMed supplement until C is live.

Spend: 394 works screened, < $0.50; the votes are cached for a later real screen.
