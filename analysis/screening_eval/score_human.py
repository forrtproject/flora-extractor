"""Score the 75 human codings against the panel, the screen, and the pipeline.

Pass --revised to score the labels settled after the coding-rule discussion
(`revised_verdict`, see screening_prompt_proposal.md §1-2) instead of the
first-pass `your_verdict` column, which is the default.
"""
import json
import sys
from collections import Counter
from pathlib import Path

import pandas as pd

HERE = Path(__file__).resolve().parent
REVISED = "--revised" in sys.argv

H = pd.read_csv(HERE / "flora_coding_75_results.csv").fillna("")
H["case"] = H["case"].astype(str).str.zfill(2)
if REVISED:
    H["your_verdict"] = H["revised_verdict"]
print("labels:", "revised (post-rule-discussion)" if REVISED else "first pass")
sheet = pd.read_csv(HERE / "coding_sheet_75.csv").fillna("")
sheet["case"] = sheet["case"].astype(str).str.zfill(2)
m = H.merge(sheet[["case", "panel_said", "doi_r", "title"]], on="case",
            how="left", suffixes=("", "_s"))

coded = m[m.your_verdict != ""]
print(f"coded {len(coded)}/{len(m)}")
print(coded.groupby("block").size().to_string(), "\n")

# ── Block A: does the Sonnet panel hold up? ─────────────────────────────────
A = coded[coded.block == "A_calibration"]
if len(A):
    agree = (A.your_verdict == A.panel_said).sum()
    print("═" * 68)
    print(f"BLOCK A — panel calibration (n={len(A)})")
    print(f"  human agrees with Sonnet panel: {agree}/{len(A)} = {agree/len(A):.0%}")
    dis = A[A.your_verdict != A.panel_said]
    if len(dis):
        print("  disagreements (human vs panel):")
        for _, r in dis.iterrows():
            print(f"    {r['ref']}: human={r.your_verdict}/{r.your_confidence} "
                  f"panel={r.panel_said}  {str(r.get('title',''))[:60]}")
            if r.your_note:
                print(f"        note: {r.your_note[:140]}")
    print()

# ── Blocks B/C: what the screen did vs the human ───────────────────────────
for blk, label, screen_said in (
        ("B_discarded", "the screen DISCARDED these as not-a-replication", "no"),
        ("C_disagreement", "the two screen models SPLIT on these", None)):
    D = coded[coded.block == blk]
    if not len(D):
        continue
    print("═" * 68)
    print(f"{blk} — {label} (n={len(D)})")
    print("  human verdicts:", dict(Counter(D.your_verdict)))
    if screen_said:
        wrong = D[D.your_verdict == "yes"]
        print(f"  → WRONGLY DISCARDED: {len(wrong)}/{len(D)} = {len(wrong)/len(D):.0%}")
        hi = wrong[wrong.your_confidence == "high"]
        print(f"     of which human was high-confidence: {len(hi)}")
        for _, r in wrong.iterrows():
            print(f"     · [{r.your_confidence}] {str(r.get('title',''))[:70]}")
            if r.your_note:
                print(f"         {r.your_note[:150]}")
    else:
        y = (D.your_verdict == "yes").sum()
        print(f"  → genuine replications parked in the set-aside pile: "
              f"{y}/{len(D)} = {y/len(D):.0%}")
        for _, r in D[D.your_verdict == "yes"].iterrows():
            print(f"     · [{r.your_confidence}] {str(r.get('title',''))[:70]}")
    print()

# ── Block D: link accuracy ─────────────────────────────────────────────────
Dd = coded[coded.block == "D_link_accuracy"]
if len(Dd):
    print("═" * 68)
    print(f"BLOCK D — llm_references link accuracy (n={len(Dd)})")
    print("  verdicts:", dict(Counter(Dd.your_verdict)))
    ok = (Dd.your_verdict == "yes").sum()
    print(f"  → correct original: {ok}/{len(Dd)} = {ok/len(Dd):.0%}")
    for _, r in Dd[Dd.your_verdict != "yes"].iterrows():
        print(f"     · {r.your_verdict} [{r.your_confidence}] {str(r.get('title',''))[:65]}")
        if r.your_note:
            print(f"         {r.your_note[:150]}")
    print()

# ── Extrapolation ──────────────────────────────────────────────────────────
print("═" * 68)
print("EXTRAPOLATION to the full pools")
B = coded[coded.block == "B_discarded"]
C = coded[coded.block == "C_disagreement"]
if len(B):
    p = (B.your_verdict == "yes").mean()
    lo, hi = _ci = (max(0, p - 1.96 * (p * (1 - p) / len(B)) ** .5),
                    min(1, p + 1.96 * (p * (1 - p) / len(B)) ** .5))
    print(f"  discard pool (285 rows): {p:.0%} wrong  → ~{p*285:.0f} lost "
          f"[95% CI {lo*285:.0f}-{hi*285:.0f}]")
if len(C):
    p = (C.your_verdict == "yes").mean()
    print(f"  disagreement pool (125 rows): {p:.0%} genuine → ~{p*125:.0f} recoverable")
if len(Dd):
    p = (Dd.your_verdict == "yes").mean()
    print(f"  llm_references (43 rows): {p:.0%} correct → ~{(1-p)*43:.0f} bad links "
          f"would enter the DB via PR 1")
