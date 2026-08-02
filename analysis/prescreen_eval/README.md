# Pre-screen evaluation (issue #130)

Does a pair of very small models, allowed only to discard and only when both agree, save
enough of Stage 3's screening bill to be worth the papers it loses?

## What is in here

| file | what it is |
| --- | --- |
| `build_casesets.py` | rebuilds the four buckets from `data/` — see `CASESETS.md`. Its intermediate `cases_*.json` and `specials_raw.json` are not committed; run it, then `enrich_casesets.py` and `rebuild_curated_neg.py`, to regenerate them |
| `enrich_casesets.py` | one pass over `filtered.csv` to attach Stage 2's verdict to every case |
| `rebuild_curated_neg.py` | rebuilds the curated-negative bucket from rows that actually reach Stage 3 |
| `cases_live_*.json` | the buckets, restricted to rows Stage 2 does not already kill — **the eval population** |
| `prompt_p1..p4.txt` | the four prompt variants |
| `eval_prescreen.py` | runs one prompt × one model over case sets; resumable |
| `score_prescreen.py` | scores a configuration: discard rate, miss rate, Wilson interval |
| `pre_<prompt>_<model>_<set>.json` | one voter's answers |
| `REPORT.md` | the findings |

## The population, and why `cases_live_*` is the only honest one

Rows Stage 2 marks `false_positive` never reach Stage 3 (`run_extract._process_row`), so
scoring them as negatives the pre-screen "saved" measures nothing. Measured over
`data/filtered.csv` on 2026-08-02:

```
total                       2,581,092
false_positive (all conf.)  2,532,538   98.1%   ← never reaches Stage 3
replication / reproduction     48,537    1.9%
needs_review                       17
```

So Stage 3 sees ~49,800 rows, not the ~180,000 the issue assumes, and essentially all of
them carry Stage 2's *high-confidence* `replication` verdict — including every one of the
184 screen-confirmed negatives. That kills the idea of bypassing the pre-screen on Stage
2's verdict: it would bypass the entire population.

## Running it

```bash
set -a; source ~/.claude/api_keys.env; set +a
python3 eval_prescreen.py <model-id> prompt_p3.txt cases_live_goldneg_screen.json --workers=3
python3 score_prescreen.py p3 google/gemini-2.5-flash-lite mistralai/mistral-nemo \
        --split=dev --bypass=all --losses
```

Keep `--workers` low. OpenRouter's cheapest-provider routing pins a single upstream
provider that rate-limits well below what this eval wants.

## Two rules the numbers depend on

**Select on `dev`, report `test`.** These 1,486 cases are the same rows that motivated
the design, so a prompt chosen on all of them carries a winner's curse. The split is a
hash of the case id.

**Nothing but an explicit, parsed "no" is a discard.** An API error, an unreadable reply
and an unrecognised label all proceed, in the scorer and in `shared/prescreen.py` alike.
The tier fails open or it is not safe to run at all.
