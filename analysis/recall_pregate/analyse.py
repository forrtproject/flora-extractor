"""Score candidate pre-fetch narrowing rules: volume kept vs positives kept.

Inputs (all under cache/, produced by the sibling scripts):
  sample/*.parquet          sample_snapshot.py — PPS row-group sample of the snapshot
  positives.parquet         fetch_positives.py — OpenAlex records of the positive sets
  F_group.csv               the 417 Observatory cause-F works + their recovered text flags
  probe_sample_noabs.parquet probe_sources.py — Europe PMC hit/stem rates on the universe

The UNIVERSE is what a pre-gate fetch would have to consider: snapshot works with no
abstract that the gate does not already admit on title or concept. Volumes are scaled
to the full snapshot (510,372,821 records, manifest 2026-06-26) by the sample fraction.

Recall is measured on:
  gap        165 cause-F Observatory works, not already ours, whose recovered abstract
             fires the gate stem — the population step 7 is about
  gap_proc   the 148 of them the shipped screen then says `proceed` on
  flora_na   FLoRA + extracted replications that have NO OpenAlex abstract today
  flora_sim  every FLoRA + extracted replication, abstract ignored (simulated missing)

    .venv/bin/python -m analysis.recall_pregate.analyse
Writes cache/rules.csv and prints the markdown tables used in REPORT.md.
"""

from pathlib import Path

import pandas as pd

HERE = Path(__file__).resolve().parent
C = HERE / "cache"
SNAPSHOT_RECORDS = 510_372_821

TYPES = {"article", "preprint", "review", "report"}
DOMAINS = {"Social Sciences", "Life Sciences", "Health Sciences"}

# Each rule is a predicate over the shared feature frame. Stacked in this order.
RULES = [
    ("has DOI", lambda d: d.doi.notna()),
    ("type in article/preprint/review/report", lambda d: d.type.isin(TYPES)),
    ("not paratext, not retracted", lambda d: ~d.is_paratext.fillna(False) & ~d.is_retracted.fillna(False)),
    ("has >=1 reference", lambda d: d.refs > 0),
    ("domain Social/Life/Health", lambda d: d.domain.isin(DOMAINS)),
    ("year >= 1970", lambda d: d.year.fillna(0) >= 1970),
    ("language en", lambda d: d.language.fillna("en") == "en"),
    ("has PMID", lambda d: d.has_pmid),
]


def md(df: pd.DataFrame) -> str:
    """A markdown table without the optional `tabulate` dependency."""
    head = "| " + " | ".join(map(str, df.columns)) + " |"
    rule = "|" + "|".join("---" for _ in df.columns) + "|"
    body = ["| " + " | ".join(map(str, r)) + " |" for r in df.itertuples(index=False)]
    return "\n".join([head, rule, *body])


def load() -> tuple[pd.DataFrame, dict[str, pd.DataFrame]]:
    s = pd.read_parquet(C / "sample")
    uni = s[~s.has_abstract & ~(s.hit_title | s.hit_concept)].copy()
    p = pd.read_parquet(C / "positives.parquet")
    f = pd.read_csv(C / "F_group.csv")
    gap = f[(~f.ours) & f.stem]
    pos = {
        "gap": p[p.query_doi.isin(gap.doi)],
        "gap_proc": p[p.query_doi.isin(gap[gap.screen_verdict == "proceed"].doi)],
        "flora_na": p[p.set.isin(["flora", "extracted"]) & ~p.has_abstract].drop_duplicates("doi"),
        "flora_sim": p[p.set.isin(["flora", "extracted"])].drop_duplicates("doi"),
    }
    return s, uni, pos


def main() -> None:
    s, uni, pos = load()
    scale = SNAPSHOT_RECORDS / len(s)
    print(f"sample rows {len(s):,}  ({len(s) / SNAPSHOT_RECORDS:.2%} of the snapshot)")
    print(f"no abstract: {(~s.has_abstract).mean():.1%} -> {(~s.has_abstract).sum() * scale / 1e6:.0f}M; "
          f"universe (no abstract, not admitted on title/concept): {len(uni) * scale / 1e6:.0f}M")
    gate = s.hit_title | s.hit_abstract | s.hit_concept
    print(f"gate pass rate in sample {gate.mean():.3%} -> {gate.sum() * scale / 1e6:.2f}M")

    probe = pd.read_parquet(C / "probe_sample_noabs.parquet") if (C / "probe_sample_noabs.parquet").exists() else None

    rows = []
    mask_u = pd.Series(True, index=uni.index)
    masks_p = {k: pd.Series(True, index=v.index) for k, v in pos.items()}
    for name, pred in [("(universe)", None)] + RULES:
        if pred is not None:
            mask_u &= pred(uni)
            for k, v in pos.items():
                masks_p[k] &= pred(v)
        kept = uni[mask_u]
        row = {"rule (cumulative)": name, "volume_M": round(len(kept) * scale / 1e6, 2),
               "pmid_share": round(kept.has_pmid.mean(), 3) if len(kept) else 0}
        for k, v in pos.items():
            row[k] = f"{int(masks_p[k].sum())}/{len(v)}"
        # The probe was drawn inside the narrowed universe, so its rates only describe
        # the last two rows; applying them to wider rows would be extrapolation.
        if probe is not None and len(kept) and name in (RULES[-2][0], RULES[-1][0]):
            # Expected new gate admissions = volume x P(EPMC abstract) x P(stem | abstract),
            # rates taken per has_pmid stratum from the probe and re-weighted to the mix kept.
            exp = 0.0
            for flag, grp in kept.groupby("has_pmid"):
                pg = probe[probe.has_pmid == flag]
                if len(pg):
                    exp += len(grp) * scale * pg.epmc_stem.mean()
            row["exp_new_admits_k"] = round(exp / 1e3, 1)
        rows.append(row)
    out = pd.DataFrame(rows)
    out.to_csv(C / "rules.csv", index=False)
    print(md(out))

    # One-at-a-time: what each rule alone removes from the universe and from the positives.
    single = []
    for name, pred in RULES:
        m = pred(uni)
        r = {"rule alone": name, "universe kept": f"{m.mean():.1%}"}
        for k, v in pos.items():
            r[k] = f"{pred(v).mean():.1%}"
        single.append(r)
    print(md(pd.DataFrame(single)))

    if probe is not None:
        print(probe.groupby("has_pmid")[["epmc_hit", "epmc_stem"]].agg(["mean", "sum", "count"]))


if __name__ == "__main__":
    main()
