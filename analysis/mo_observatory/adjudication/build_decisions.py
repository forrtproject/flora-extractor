"""One recommended action per work where the Observatory names a different original
from ours — the sheet a human confirms before anything is corrected.

Two populations, each in two kinds:
  pipeline vs Observatory — our Stage 3 rows (`data/extracted.csv`).
  FLoRA vs Observatory    — shipped, human-curated FLoRA (`data/flora.csv`).
  different paper — decided by the two blinded judges (Claude + Codex; gpt-5.6-sol for
                    the pipeline items, gpt-6-sol for FLoRA's).
  DOI: …          — the same paper under different DOIs, classified by `doi_pairs.py` as a
                    MISTAKE (the DOI points to something that is not the work) or an
                    ALTERNATIVE IDENTIFIER (preprint, working paper, duplicate registration).

Writes `decisions.csv` and `decisions.html`.

    .venv/bin/python -m analysis.mo_observatory.adjudication.build_decisions
"""
from __future__ import annotations

import html
import re
from pathlib import Path

import pandas as pd

from analysis.mo_observatory.adjudication.agreement import load
from analysis.mo_observatory.adjudication.build_report import verdicts
from shared.utils import clean_doi

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[2]
_DOI_URL = r"^https?://(dx\.)?doi\.org/"


def _mo_independent() -> pd.DataFrame:
    """The Observatory's rows that were not imported from FLoRA."""
    mo = pd.read_csv(HERE.parent / "replications_database_2026_09_04_184008.csv", dtype=str).fillna("")
    mo = mo[~mo.source.str.contains("FLoRa", case=False)]
    mo["doi_r"] = mo.replication_url.str.replace(_DOI_URL, "", regex=True).map(clean_doi)
    mo["doi_o"] = mo.original_url.str.replace(_DOI_URL, "", regex=True).map(clean_doi)
    return mo

ACTION_ORDER = ["replace", "keep", "keep + add", "human", "fix DOI", "prefer journal DOI", "no change"]


def doi_decision(r) -> tuple[str, str]:
    """Action for a same-title pair, from doi_pairs.py's classes. Side A is ours/FLoRA's."""
    if r.pair == "same DOI, formatting":
        return ("fix DOI" if "side A" in r.detail else "no change"), r.detail
    if r.pair == "mistake":
        if r.class_a == "mistake":
            return "fix DOI", r.detail
        return "no change", r.detail + " — the error is the Observatory's"
    if r.class_a == "alternative" and r.class_b == "work":
        return "prefer journal DOI", r.detail
    if r.pair == "unresolved":
        return "no change", r.detail
    return "no change", r.detail


def judged_decision(r: dict) -> tuple[str, str]:
    c, x = r["claude_verdict"], r["codex_verdict"]
    low = "low" in (r["claude_confidence"], r["codex_confidence"])
    if c != x:
        return "human", f"judges split (Claude: {c}, Codex: {x})"
    rule = {"mo": "replace", "ours": "keep", "both": "keep + add"}.get(c)
    if rule is None:
        return "human", f"both judges: {c}"
    return rule, "both judges agree" + (" (one at low confidence — check)" if low else "")


def main() -> None:
    ours, mo = load()
    rows = []
    for pop, v in (("pipeline vs Observatory", verdicts()),
                   ("FLoRA vs Observatory", verdicts("key_flora.csv", "codex6"))):
      for r in v[v.stratum == "original"].to_dict("records"):
        r = {**r, **{f"{j}_verdict": {"flora": "ours"}.get(r[f"{j}_verdict"], r[f"{j}_verdict"])
                     for j in ("claude", "codex")}}
        action, why = judged_decision(r)
        rows.append({"doi_r": r["doi_r"], "population": pop, "kind": "different paper", "item": r["id"],
                     "action": action, "why": why,
                     "claude": f"{r['claude_verdict']} ({r['claude_confidence']}): {r['claude_reasoning']}",
                     "codex": f"{r['codex_verdict']} ({r['codex_confidence']}): {r['codex_reasoning']}",
                     "suggested_instead": r["claude_correct_original"] or r["codex_correct_original"]})
    for r in pd.read_csv(HERE / "doi_pairs.csv", dtype=str).fillna("").itertuples():
        action, why = doi_decision(r)
        kind = ("DOI: " + ("mistake" if r.pair == "mistake" else
                           "formatting" if r.pair.startswith("same DOI") else
                           "missing" if r.pair == "unresolved" else "alternative identifier"))
        rows.append({"doi_r": r.doi_r, "population": r.population, "kind": kind, "item": "", "action": action,
                     "why": why, "claude": "", "codex": "", "suggested_instead": "",
                     "_a": f"{r.title_a} doi:{r.doi_a}", "_b": f"{r.title_b} doi:{r.doi_b}"})
    d = pd.DataFrame(rows)
    fl = pd.read_csv(ROOT / "data/flora.csv", dtype=str, encoding="utf-8-sig").fillna("")
    fl["doi_r"], fl["doi_o"] = fl.doi_r.map(clean_doi), fl.doi_o.map(clean_doi)
    mo_ind = _mo_independent()
    ctx = []
    for r in d.itertuples():
        if r.population.startswith("FLoRA"):
            f, m = fl[fl.doi_r == r.doi_r], mo_ind[mo_ind.doi_r == r.doi_r].drop_duplicates("doi_o")
            ctx.append({"title_r": f.title_r.iloc[0], "year_r": f.year_r.iloc[0],
                        "our_originals": " || ".join(f"{x.title_o} ({x.year_o}) doi:{x.doi_o} [FLoRA]"
                                                     for x in f.drop_duplicates("doi_o").itertuples()),
                        "mo_originals": " || ".join(f"{x.original_title} ({x.original_year}) doi:{x.doi_o}"
                                                    for x in m.itertuples())})
            continue
        o, m = ours[ours.doi_r == r.doi_r], mo[mo.doi_r == r.doi_r].drop_duplicates("doi_o")
        ctx.append({"title_r": o.title_r.iloc[0], "year_r": o.year_r.iloc[0],
                    "our_originals": " || ".join(f"{x.title_o} ({x.year_o}) doi:{x.doi_o} [{x.link_method}]"
                                                 for x in o.drop_duplicates("doi_o").itertuples()),
                    "mo_originals": " || ".join(f"{x.original_title} ({x.original_year}) doi:{x.doi_o}"
                                                for x in m.itertuples())})
    d = pd.concat([d, pd.DataFrame(ctx)], axis=1).drop(columns=["_a", "_b"], errors="ignore")
    d["_o"] = d.action.map({a: i for i, a in enumerate(ACTION_ORDER)})
    d = d.sort_values(["population", "_o", "kind", "item"], ascending=[False, True, True, True]).drop(columns="_o")
    d.insert(0, "your_decision", "")
    d.to_csv(HERE / "decisions.csv", index=False, encoding="utf-8-sig")
    print(pd.crosstab([d.population, d.kind], d.action, margins=True).to_string())
    _html(d)


def _html(d: pd.DataFrame) -> None:
    e = lambda s: html.escape(str(s or ""))
    link = lambda s: re.sub(r"doi:(\S+?)(?=\s|\]|$)", lambda m: f"<a href='https://doi.org/{m.group(1)}'>{m.group(1)}</a>", e(s))
    trs = []
    for r in d.itertuples():
        judges = (f"<details><summary>judges</summary><p><b>Claude</b> {e(r.claude)}</p><p><b>Codex</b> {e(r.codex)}</p>"
                  + (f"<p><b>Suggested instead:</b> {e(r.suggested_instead)}</p>" if r.suggested_instead else "")
                  + "</details>") if r.claude else ""
        trs.append(f"<tr class='a-{r.action.replace(' + ', '-').replace(' ', '-')}'><td><b>{e(r.action)}</b><br><span class=m>{e(r.why)}</span>{judges}</td>"
                   f"<td>{e(r.title_r)} ({e(r.year_r)})<br><a href='https://doi.org/{e(r.doi_r)}'>{e(r.doi_r)}</a>"
                   f"<br><span class=m>{e(r.population)} · {e(r.kind)} {e(r.item)}</span></td>"
                   f"<td>{link(r.our_originals).replace(' || ', '<br>')}</td><td>{link(r.mo_originals).replace(' || ', '<br>')}</td></tr>")
    counts = d.action.value_counts()
    page = f"""<!doctype html><html><head><meta charset='utf-8'><title>Original-study decisions</title><style>
body{{font:14px/1.45 system-ui,sans-serif;margin:2em;color:#222}}table{{border-collapse:collapse;width:100%}}
td,th{{border:1px solid #ddd;padding:.4em;vertical-align:top;text-align:left}}th{{background:#f4f4f4;position:sticky;top:0}}
.m{{color:#777;font-size:.85em}}td:first-child{{width:22%}}td:nth-child(2){{width:24%}}
.a-replace td:first-child{{background:#ffe6c7}}.a-keep td:first-child{{background:#d7ecff}}.a-keep-add td:first-child{{background:#e2f5e2}}
.a-human td:first-child{{background:#fff3a8}}.a-no-change td:first-child{{background:#f2f2f2}}</style></head><body>
<h1>Where the Observatory names a different original: one decision per paper</h1>
<p>Sections: <b>FLoRA vs Observatory</b> (shipped, human-curated FLoRA) first, then <b>pipeline vs Observatory</b>
(our unvalidated Stage 3 rows).</p>
<p>{len(d)} papers. Recommended: {', '.join(f'<b>{counts.get(a, 0)}</b> {a}' for a in ACTION_ORDER)}.
<b>replace</b> = the Observatory's original is right and ours is not; <b>keep</b> = ours is right;
<b>keep + add</b> = both are originals this paper re-tests; <b>human</b> = the judges split or could not tell;
<b>fix DOI</b> = our/FLoRA's DOI is a mistake (points to an erratum, a reply, another paper, or is malformed);
<b>prefer journal DOI</b> = ours is an alternative identifier (preprint, working paper, JSTOR copy) of the article;
<b>no change</b> = our DOI is the article of record (any mistake or alternative identifier is the Observatory's). The same list is in
<code>decisions.csv</code> with an empty <code>your_decision</code> column.</p>
<table><tr><th>recommendation</th><th>replication paper</th><th>our / FLoRA's original(s)</th><th>Observatory's original(s)</th></tr>
{''.join(trs)}</table></body></html>"""
    (HERE / "decisions.html").write_text(page, encoding="utf-8")


if __name__ == "__main__":
    main()
