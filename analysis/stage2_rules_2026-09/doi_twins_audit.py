"""Issue #210 audit: OpenAlex records whose title/year disagree with their DOI's registry.

Population: every work in an admitted pile of release c048ab6483d3 (screen_expensive
+ screen_cheap + needs_human — the last two are empty on that release) and every work
Stage 3 has written, in `data/extracted.csv` and its set-aside CSVs. For each, the
DOI's registry record (`registry.py`, cached, Crossref then doi.org) is compared with
the OpenAlex title and year (`twin_compare.py`).

The pool side is the POOL's title/year (by raw work id), not the CSV's `title_r`, so
both populations are compared on what OpenAlex holds. Also recorded: whether another
pool record carries the same DOI and whether THAT one matches the registry — the
"real paper is a sibling" corroboration.

Writes `doi_twins_audit.csv` (every compared work) and prints the counts.

    PYTHONPATH=. .venv/bin/python analysis/stage2_rules_2026-09/doi_twins_audit.py [--fetch]
"""

import argparse
import json
import re
import sys
from pathlib import Path

import pandas as pd

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from registry import cached_only, fetch_many  # noqa: E402
from twin_compare import latin_share, similarity, year_gap  # noqa: E402

from filter.engine.workids import work_id  # noqa: E402

ROOT = HERE.parents[1]
SCRATCH = ROOT / "cache" / "stage2_rules_2026-09"
SETASIDE = ("api_error.csv", "keyed_link_disputed.csv", "no_evidence.csv",
            "no_original_found.csv", "not_a_replication.csv",
            "prospective_registration.csv", "search_link_unconfirmed.csv",
            "target_pending.csv", "unidentified_original.csv")
KNOWN = ROOT / "analysis" / "mo_observatory" / "doi_title_mismatch.csv"

# The flag. Tuned on the 30 known twins (all score 0.0) against the admitted pile
# and extracted works; see REPORT.md, "Threshold".
SIM_MAX = 0.5

# The OpenAlex title making a replication/reproduction claim of its own. A mismatch
# on such a record is the OTHER direction of the same fault — a real study carrying a
# wrong DOI — and discarding it would lose the study, so it is not a twin record.
# The stems are `replication-signal`'s title arm (filter/spec/replication-signal.json).
_OWN_CLAIM = re.compile(r"replicat|replicab|reproduc|reanalys|re-analys|reanalyz|re-analyz|"
                        r"replikat|réplicat|replicaci|replicaç|replicazion|reproduç|"
                        r"reproduzi|追試|반복검증|재검증", re.I)


def verdict(sim: float, gap, oa_latin: float, reg_latin: float, sib_match: bool) -> str:
    if sim != sim:
        return "no_title"
    if sim > SIM_MAX:
        return "match"
    # A title in another script than the registry's is a translation until the year
    # says otherwise: the registry may carry only the English title.
    script_differs = abs(oa_latin - reg_latin) > 0.5
    if script_differs and (gap is None or gap <= 1) and not sib_match:
        return "translation_suspect"
    return "mismatch"


def twin_class(v: str, oa_title: str) -> str:
    """Which way round a registry mismatch is: see REPORT.md, "Two faults"."""
    if v != "mismatch":
        return ""
    if oa_title.strip().upper() == "WITHDRAWN":
        return "withdrawn_record"
    return "wrong_doi_on_real_study" if _OWN_CLAIM.search(oa_title) else "twin_record"


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--fetch", action="store_true", help="Fetch registry records not yet cached.")
    ap.add_argument("--spec", type=Path, default=None,
                    help="Write the doi-registry-twin spec (twin_record works) to this path.")
    args = ap.parse_args()

    pool = pd.read_parquet(SCRATCH / "pool_dois.parquet")
    pool["raw_work_id"] = pool["id"].map(work_id)
    pool["pool_title"] = pool["display_name"].fillna(pool["title"]).fillna("")
    by_raw = pool.set_index("raw_work_id")

    admitted = pd.read_parquet(SCRATCH / "admitted_c048.parquet")
    rows = [{"raw_work_id": int(r.raw_work_id), "population": "admitted_c048"}
            for r in admitted.itertuples() if r.doi]
    for f in ["extracted.csv", *SETASIDE]:
        d = pd.read_csv(ROOT / "data" / f, dtype=str, usecols=["openalex_id_r", "doi_r"])
        for r in d.dropna(subset=["openalex_id_r"]).itertuples():
            rows.append({"raw_work_id": work_id(r.openalex_id_r),
                         "population": "extracted" if f == "extracted.csv" else "set_aside:" + f})
    pop = (pd.DataFrame(rows).groupby("raw_work_id")["population"]
           .agg(lambda s: "|".join(sorted(set(s)))).reset_index())
    pop = pop[pop.raw_work_id.isin(by_raw.index)]
    pop = pop.join(by_raw[["cdoi", "pool_title", "publication_year"]], on="raw_work_id")
    pop = pop[~pop.index.duplicated()]

    if args.fetch:
        fetch_many(set(pop.cdoi), workers=3)

    sibs = pool.groupby("cdoi")["raw_work_id"].apply(list)
    known = set(pd.read_csv(KNOWN).work_id.astype(int)) if KNOWN.exists() else set()
    out = []
    for r in pop.itertuples():
        meta = cached_only(r.cdoi)
        rec = {"work_id": r.raw_work_id, "doi": r.cdoi, "population": r.population,
               "pool_year": r.publication_year, "pool_title": r.pool_title,
               "known_twin": r.raw_work_id in known}
        if meta is None:
            rec["verdict"] = "registry_unanswered"
            out.append(rec)
            continue
        if not meta.get("registered"):
            rec.update(verdict="doi_unregistered")
            out.append(rec)
            continue
        reg_title = (meta.get("titles") or [""])[0]
        sim = similarity(r.pool_title, meta)
        gap = year_gap(r.publication_year, meta)
        others = [w for w in sibs.get(r.cdoi, []) if w != r.raw_work_id]
        sib_match = any(similarity(by_raw.loc[w, "pool_title"] if not isinstance(
            by_raw.loc[w, "pool_title"], pd.Series) else by_raw.loc[w, "pool_title"].iloc[0],
            meta) > SIM_MAX for w in others)
        v = verdict(sim, gap, latin_share(r.pool_title), latin_share(reg_title), sib_match)
        cls = twin_class(v, r.pool_title)
        rec.update(registry_agency=meta.get("agency"), registry_type=meta.get("type"),
                   registry_title=reg_title, registry_years=";".join(
                       f"{k}={y}" for k, y in (meta.get("years") or {}).items()),
                   title_sim=round(sim, 3) if sim == sim else None, year_gap=gap,
                   n_pool_siblings=len(others), sibling_matches_registry=sib_match,
                   verdict=v, twin_class=cls)
        out.append(rec)
    res = pd.DataFrame(out).sort_values(["verdict", "title_sim", "work_id"])
    res.to_csv(HERE / "doi_twins_audit.csv", index=False, encoding="utf-8-sig")

    print("works compared:", len(res))
    res["in_extracted"] = res.population.str.contains("extracted|set_aside")
    print(pd.crosstab(res.in_extracted, res.verdict, margins=True).to_string())
    mm = res[res.verdict == "mismatch"]
    print("\nmismatches by class (all / known 30 / in extracted+set-asides / with a sibling "
          "matching the registry):")
    for cls, grp in mm.groupby("twin_class"):
        print(f"  {cls:26} {len(grp):3}  {int(grp.known_twin.sum()):3}  "
              f"{int(grp.in_extracted.sum()):3}  {int(grp.sibling_matches_registry.sum()):3}")
    print("known twins flagged:", int(res[res.known_twin].verdict.eq("mismatch").sum()), "of",
          int(res.known_twin.sum()))
    if args.spec:
        twins = sorted(int(w) for w in mm.work_id[mm.twin_class == "twin_record"])
        args.spec.write_text(json.dumps(_spec(twins), indent=2, ensure_ascii=False) + "\n",
                             encoding="utf-8")
        print("wrote", args.spec, len(twins), "records")


def _spec(twins: list[int]) -> dict:
    return {
        "id": "doi-registry-twin",
        "description": (
            "SHADOW DISCARD, issue #210: OpenAlex records whose DOI's registry record "
            "(Crossref, else doi.org content negotiation) names a DIFFERENT paper — "
            "title overlap coefficient <= 0.5 against every registry title (main, "
            "main+subtitle, original-language, short), a different script counted as "
            "a translation unless the year is > 1 apart or another pool record under "
            "the same DOI carries the registry's title — AND whose own title makes no "
            "replication claim (a mismatched record that does is a real study with a "
            "wrong DOI, class `wrong_doi_on_real_study`, and is NOT listed). A list of "
            "records, not a pattern, because a spec cannot read the registry at route "
            "time: the list is DERIVED, like aliases.json, by `PYTHONPATH=. "
            ".venv/bin/python analysis/stage2_rules_2026-09/doi_twins_audit.py --fetch "
            "--spec <path>` over one release's admitted piles and the extracted works, "
            "and is regenerated whenever the admitted population changes (a new "
            "release that admits works the last audit did not see). Precedence 950 "
            "outranks every admission; it matters only once promoted. Measured "
            "2026-09-23 over release c048ab6483d3: all 30 known twins "
            "(analysis/mo_observatory/doi_title_mismatch.csv) score 0.0 and every "
            "matching record scores >= 0.8 — see analysis/stage2_rules_2026-09/REPORT.md."),
        "match": {"work_id_in": twins},
        "pile": "discard",
        "vocabulary": None,
        "precedence": 950,
        "shadow": True,
        "measured": [],
    }

if __name__ == "__main__":
    main()
