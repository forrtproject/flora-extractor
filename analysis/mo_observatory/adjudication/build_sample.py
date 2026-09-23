"""The blinded adjudication sample: a stratified draw of the works where our Stage 3 rows
and the Observatory diverge, each written out as a self-contained evidence packet.

Blinding: the two answers are labelled A and B, assigned at random per item; the key
(`key.csv`) is kept out of `packets/`. A packet carries the replication paper (full text
when the parse cache holds it) and the two answers — nothing from either side's
reasoning, which would name its source.

Strata:
  original      — no original in common, and not the same title under another DOI
  contradiction — same original, success vs failure (plus the reversals)
  inconclusive  — same original, success/failure vs inconclusive
  cbd           — same original, we say cannot_be_determined, they give a verdict

    .venv/bin/python -m analysis.mo_observatory.adjudication.build_sample
"""
from __future__ import annotations

import json
import random
from pathlib import Path

import pandas as pd

from analysis.mo_observatory.adjudication.agreement import load
from shared.disambiguation import jaccard_similarity
from shared.pdf_parsing import best_parse_result, read_parse_cache

HERE = Path(__file__).resolve().parent
PACKETS = HERE / "packets"
SEED = 20260922
QUOTAS = {"original": 40, "contradiction": 99, "inconclusive": 30, "cbd": 20}
SAME_TITLE = 0.95
FULLTEXT_CAP = 60_000
OURS_TO_SHOWN = {"successful": "success", "failed": "failure", "mixed": "inconclusive (mixed)",
                 "statistically successful but flawed": "inconclusive (significant but flawed)",
                 "cannot_be_determined": "cannot be determined from the paper"}


def _people(s: str) -> str:
    try:
        v = json.loads(s)
        return "; ".join(f"{a.get('given', '')} {a.get('family', '')}".strip() for a in v)
    except (ValueError, TypeError, AttributeError):
        return s


def _fulltext(doi_r: str) -> str:
    try:
        parsed = read_parse_cache(doi_r)
    except ValueError:
        return ""
    best = best_parse_result(parsed) if parsed else None
    return ((best or {}).get("raw_text") or "")[:FULLTEXT_CAP]


def strata(ours: pd.DataFrame, mo: pd.DataFrame) -> pd.DataFrame:
    wk = pd.read_csv(HERE / "works.csv", dtype=str)
    pr = pd.read_csv(HERE / "pairs.csv", dtype=str).fillna("")
    items = []
    for w in wk[wk.original.str.startswith("different")].doi_r:
        o, m = ours[ours.doi_r == w], mo[mo.doi_r == w].drop_duplicates("doi_o")
        sim = max(jaccard_similarity(a, b) for a in o.title_o for b in m.original_title)
        items.append({"doi_r": w, "stratum": "original" if sim < SAME_TITLE else "same_title", "doi_o": ""})
    kinds = {"contradiction": pr.kind.str.startswith(("contradiction", "reversal")),
             "inconclusive": pr.kind.str.startswith("inconclusive"),
             "cbd": pr.kind == "ours: cannot_be_determined"}
    for s, mask in kinds.items():
        items += [{"doi_r": r.doi_r, "stratum": s, "doi_o": r.doi_o} for r in pr[mask].itertuples()]
    return pd.DataFrame(items)


def write_packet(iid: str, it, ours: pd.DataFrame, mo: pd.DataFrame, ours_first: bool) -> bool:
    """Write one blinded packet; returns whether it carries full text."""
    o = ours[ours.doi_r == it.doi_r]
    m = mo[mo.doi_r == it.doi_r]
    rep = o.iloc[0]
    ft = _fulltext(it.doi_r)
    lines = [f"# Item {iid}", "",
             "## The replication paper", "",
             f"- DOI: {it.doi_r} (https://doi.org/{it.doi_r})",
             f"- Title: {rep.title_r}", f"- Authors: {_people(rep.authors_r)}",
             f"- Year: {rep.year_r}  ·  Journal: {rep.journal_r}", "",
             "### Abstract", "", rep.abstract_r or "(no abstract on record)", ""]
    if it.stratum == "original":
        def side(is_ours: bool) -> list[str]:
            if is_ours:
                return [f"- {r.title_o} — {_people(r.authors_o)} ({r.year_o}), doi:{r.doi_o}"
                        for r in o.drop_duplicates("doi_o").itertuples()]
            return [f"- {r.original_title} — {r.original_authors} ({r.original_year}), doi:{r.doi_o or '(none given)'}"
                    for r in m.drop_duplicates("doi_o").itertuples()]
        lines += ["## Question: which ORIGINAL study does this paper replicate?", "",
                  "### Answer A", *side(ours_first), "", "### Answer B", *side(not ours_first), ""]
    else:
        r = o[o.doi_o == it.doi_o].iloc[0]
        mrows = m[m.doi_o == it.doi_o]
        finding = "; ".join(sorted(set(mrows.description) - {""}))[:400]
        shown_ours = OURS_TO_SHOWN.get(r.outcome, r.outcome)
        shown_mo = "|".join(sorted(set(mrows.result)))
        lines += ["## The original study (both answers agree on it)", "",
                  f"- {r.title_o} — {_people(r.authors_o)} ({r.year_o}), doi:{it.doi_o}"]
        if finding:
            lines += [f"- Finding at issue: {finding}"]
        lines += ["", "## Question: did the replication SUCCEED in reproducing the original finding?", "",
                  f"- Answer A: {shown_ours if ours_first else shown_mo}",
                  f"- Answer B: {shown_mo if ours_first else shown_ours}", ""]
    lines += ["## Full text of the replication paper", "",
              (f"(machine-extracted; may be truncated at {FULLTEXT_CAP:,} characters)\n\n{ft}" if ft
               else "(not available in this packet — consult the paper via its DOI)")]
    (PACKETS / f"{iid}.md").write_text("\n".join(lines), encoding="utf-8")
    return bool(ft)


def main() -> None:
    ours, mo = load()
    pop = strata(ours, mo)
    print(pop.stratum.value_counts().to_string())
    rng = random.Random(SEED)
    sample = pd.concat([g.sample(min(QUOTAS[s], len(g)), random_state=SEED)
                        for s, g in pop.groupby("stratum") if s in QUOTAS])
    sample = sample.drop_duplicates(["doi_r", "stratum"]).reset_index(drop=True)
    PACKETS.mkdir(exist_ok=True)
    keys = []
    for i, it in enumerate(sample.itertuples(), 1):
        iid = f"{it.stratum[:4]}-{i:03d}"
        ours_first = rng.random() < 0.5
        ft = write_packet(iid, it, ours, mo, ours_first)
        keys.append({"id": iid, "stratum": it.stratum, "doi_r": it.doi_r, "doi_o": it.doi_o,
                     "A": "ours" if ours_first else "mo", "B": "mo" if ours_first else "ours",
                     "has_fulltext": bool(ft)})
    k = pd.DataFrame(keys)
    k.to_csv(HERE / "key.csv", index=False, encoding="utf-8-sig")
    print(f"\n{len(k)} packets; full text in packet: {k.has_fulltext.sum()}")
    print(k.groupby("stratum").size().to_string())


def complete_original() -> None:
    """Add a packet for every different-original work the stratified draw left out, so each
    one gets a verdict. Appends to `key.csv` with fresh ids; existing items are untouched."""
    ours, mo = load()
    pop = strata(ours, mo)
    key = pd.read_csv(HERE / "key.csv", dtype=str)
    rest = pop[(pop.stratum == "original") & ~pop.doi_r.isin(set(key.doi_r))].drop_duplicates("doi_r")
    rng = random.Random(SEED + 1)
    n0 = key.id.str[-3:].astype(int).max()
    keys = []
    for i, it in enumerate(rest.itertuples(), n0 + 1):
        iid = f"orig-{i:03d}"
        ours_first = rng.random() < 0.5
        ft = write_packet(iid, it, ours, mo, ours_first)
        keys.append({"id": iid, "stratum": "original", "doi_r": it.doi_r, "doi_o": "",
                     "A": "ours" if ours_first else "mo", "B": "mo" if ours_first else "ours",
                     "has_fulltext": ft})
    pd.concat([key, pd.DataFrame(keys).astype(str)]).to_csv(HERE / "key.csv", index=False, encoding="utf-8-sig")
    print(f"added {len(keys)} original packets ({sum(k['has_fulltext'] for k in keys)} with full text): "
          f"{keys[0]['id']}..{keys[-1]['id']}")


if __name__ == "__main__":
    import sys
    complete_original() if "--complete-original" in sys.argv else main()
