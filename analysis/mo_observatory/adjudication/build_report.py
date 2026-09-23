"""Unblind the two judges' verdicts and render `report.html`.

Reads `key.csv` (which answer was whose), `out/claude/*.json` and `out/codex/*.json`,
the works/pairs tables from `agreement.py`, and `mo_vs_flora.csv`; writes
`verdicts.csv` (one row per item, both judges unblinded) and `report.html`.

    .venv/bin/python -m analysis.mo_observatory.adjudication.build_report
"""
from __future__ import annotations

import html
import json
import re
from pathlib import Path

import pandas as pd

from analysis.mo_observatory.adjudication.agreement import load

HERE = Path(__file__).resolve().parent
JUDGES = {"claude": "Claude (Opus 5.5)", "codex": "Codex (gpt-5.6-sol)"}
SIDE = {"ours": "FLoRA pipeline", "mo": "Observatory"}
STRATA = {
    "original": "Different original study",
    "contradiction": "Same original, success vs failure",
    "inconclusive": "Same original, success/failure vs inconclusive",
    "cbd": "Same original, we said “cannot be determined”",
}
VERDICT_ORDER = ["ours", "mo", "both", "neither", "cannot_tell", "missing"]
VERDICT_LABEL = {"ours": "FLoRA right", "mo": "Observatory right", "both": "both defensible",
                 "neither": "neither", "cannot_tell": "can't tell", "missing": "no answer"}


def _read(judge: str, iid: str) -> dict:
    p = HERE / "out" / judge / f"{iid}.json"
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except ValueError:
        m = re.search(r"\{.*\}", p.read_text(encoding="utf-8"), re.S)
        return json.loads(m.group(0)) if m else {}


def _side(v: str, key: dict) -> str:
    return key.get(v, v) if v in ("A", "B") else (v or "missing")


def verdicts(key_name: str = "key.csv", codex_dir: str = "codex") -> pd.DataFrame:
    """Both judges' answers per item, unblinded through *key_name*. The FLoRA-vs-Observatory
    items were judged by a different Codex model, kept in its own directory (`codex6`)."""
    key = pd.read_csv(HERE / key_name, dtype=str)
    dirs = {"claude": "claude", "codex": codex_dir}
    rows = []
    for k in key.to_dict("records"):
        r = dict(k)
        ab = {"A": k["A"], "B": k["B"]}
        for j in JUDGES:
            a = _read(dirs[j], k["id"])
            field = "original_verdict" if k["stratum"] == "original" else "more_defensible"
            r[f"{j}_verdict"] = _side(a.get(field, ""), ab) if a else "missing"
            for f in ("own_outcome", "confidence", "evidence_basis", "reasoning", "quote", "correct_original"):
                r[f"{j}_{f}"] = a.get(f, "")
        cv, xv = r["claude_verdict"], r["codex_verdict"]
        r["consensus"] = cv if cv == xv else "split"
        rows.append(r)
    return pd.DataFrame(rows)


def _e(s: object) -> str:
    return html.escape(str(s or ""))


def _tally(v: pd.DataFrame) -> str:
    head = "".join(f"<th>{VERDICT_LABEL[c]}</th>" for c in VERDICT_ORDER if c != "missing")
    out = [f"<table class='tally'><tr><th>stratum</th><th>n</th><th>who</th>{head}<th>split</th></tr>"]
    for s, label in STRATA.items():
        g = v[v.stratum == s]
        if g.empty:
            continue
        for i, (col, who) in enumerate([("claude_verdict", JUDGES["claude"]), ("codex_verdict", JUDGES["codex"]),
                                        ("consensus", "both judges agree")]):
            cells = "".join(f"<td>{(g[col] == c).sum()}</td>" for c in VERDICT_ORDER if c != "missing")
            split = f"<td>{(g[col] == 'split').sum()}</td>" if col == "consensus" else "<td></td>"
            first = f"<td rowspan=3>{_e(label)}</td><td rowspan=3>{len(g)}</td>" if i == 0 else ""
            cls = " class='cons'" if col == "consensus" else ""
            out.append(f"<tr{cls}>{first}<td>{who}</td>{cells}{split}</tr>")
    out.append("</table>")
    return "".join(out)


def _answers(it: dict, ours: pd.DataFrame, mo: pd.DataFrame) -> str:
    o, m = ours[ours.doi_r == it["doi_r"]], mo[mo.doi_r == it["doi_r"]]
    if it["stratum"] == "original":
        oo = "<br>".join(f"{_e(r.title_o)} ({_e(r.year_o)}) <a href='https://doi.org/{_e(r.doi_o)}'>{_e(r.doi_o)}</a>"
                         f" <span class='meta'>[{_e(r.link_method)}, {_e(r.link_confidence)}]</span>"
                         for r in o.drop_duplicates("doi_o").itertuples())
        mm = "<br>".join(f"{_e(r.original_title)} ({_e(r.original_year)}) <a href='https://doi.org/{_e(r.doi_o)}'>{_e(r.doi_o)}</a>"
                         for r in m.drop_duplicates("doi_o").itertuples())
    else:
        r = o[o.doi_o == it["doi_o"]].iloc[0]
        mrows = m[m.doi_o == it["doi_o"]]
        oo = f"<b>{_e(r.outcome)}</b> <span class='meta'>[{_e(r.out_quote_source)}]</span><br><i>{_e(r.outcome_phrase)}</i>"
        mm = (f"<b>{_e('|'.join(sorted(set(mrows.result))))}</b> <span class='meta'>[{_e('|'.join(sorted(set(mrows.replication_type))))}]</span>"
              f"<br><i>{_e('; '.join(sorted(set(mrows.description) - {''}))[:300])}</i>")
        oo = f"<span class='meta'>Original: {_e(r.title_o)} ({_e(r.year_o)})</span><br>" + oo
    return (f"<div class='answers'><div><h4>{SIDE['ours']}</h4>{oo}</div>"
            f"<div><h4>{SIDE['mo']}</h4>{mm}</div></div>")


def _judge(it: dict, j: str) -> str:
    v = it[f"{j}_verdict"]
    extra = f" · own coding: {_e(it[f'{j}_own_outcome'])}" if it["stratum"] != "original" else ""
    corr = (f"<br><span class='meta'>Correct original instead: {_e(it[f'{j}_correct_original'])}</span>"
            if it[f"{j}_correct_original"] else "")
    quote = f"<blockquote>{_e(it[f'{j}_quote'])}</blockquote>" if it[f"{j}_quote"] else ""
    return (f"<div class='judge'><h4>{JUDGES[j]}: <span class='v v-{v}'>{VERDICT_LABEL.get(v, v)}</span></h4>"
            f"<span class='meta'>{_e(it[f'{j}_confidence'])} confidence · {_e(it[f'{j}_evidence_basis'])}{extra}</span>"
            f"<p>{_e(it[f'{j}_reasoning'])}</p>{quote}{corr}</div>")


def _card(it: dict, ours: pd.DataFrame, mo: pd.DataFrame) -> str:
    rep = ours[ours.doi_r == it["doi_r"]].iloc[0]
    cons = it["consensus"]
    badge = ("<span class='v v-split'>judges split — needs a human</span>" if cons == "split"
             else f"<span class='v v-{cons}'>both judges: {VERDICT_LABEL.get(cons, cons)}</span>")
    return (f"<details class='card' data-stratum='{it['stratum']}' data-cons='{cons}'>"
            f"<summary><code>{it['id']}</code> {badge} {_e(rep.title_r)} <span class='meta'>({_e(rep.year_r)})</span></summary>"
            f"<p class='meta'><a href='https://doi.org/{_e(it['doi_r'])}'>{_e(it['doi_r'])}</a> · {_e(rep.journal_r)}"
            f" · full text given to judges: {'yes' if it['has_fulltext'] in (True, 'True') else 'no (looked up online)'}</p>"
            f"{_answers(it, ours, mo)}<div class='judges'>{_judge(it, 'claude')}{_judge(it, 'codex')}</div></details>")


CSS = """
body{font:15px/1.5 system-ui,sans-serif;max-width:1100px;margin:2em auto;padding:0 1em;color:#222}
h1{font-size:1.6em}h2{margin-top:2em;border-bottom:1px solid #ddd}h4{margin:.2em 0}
table{border-collapse:collapse;margin:1em 0;font-size:.9em}td,th{border:1px solid #ddd;padding:.3em .6em;text-align:right}
td:first-child,th:first-child,td:nth-child(3){text-align:left}tr.cons{background:#f4f8ff;font-weight:600}
.card{border:1px solid #ddd;border-radius:6px;margin:.5em 0;padding:.4em .8em}.card summary{cursor:pointer}
.answers,.judges{display:grid;grid-template-columns:1fr 1fr;gap:1em;margin:.6em 0}
.answers>div{background:#fafafa;padding:.5em;border-radius:4px}.meta{color:#777;font-size:.85em}
blockquote{margin:.4em 0;padding-left:.8em;border-left:3px solid #ccc;color:#555;font-size:.9em}
.v{display:inline-block;padding:0 .5em;border-radius:3px;font-size:.85em;font-weight:600}
.v-ours{background:#d7ecff}.v-mo{background:#ffe6c7}.v-both{background:#e2f5e2}.v-neither{background:#f3d6d6}
.v-cannot_tell,.v-missing{background:#eee}.v-split{background:#fff3a8}
.filters button{margin:.2em;padding:.2em .7em;border:1px solid #bbb;border-radius:4px;background:#fff;cursor:pointer}
.filters button.on{background:#333;color:#fff}
"""
JS = """
function f(attr,val,btn){document.querySelectorAll('.card').forEach(c=>{c.style.display=(!val||c.dataset[attr]===val)?'':'none'});
document.querySelectorAll('.filters button').forEach(b=>b.classList.remove('on'));btn.classList.add('on');}
"""


def _findings(v: pd.DataFrame, ours: pd.DataFrame) -> str:
    """The headline reading of the tally, plus the two checks that say whether to trust it."""
    dec = {j: v[v[f"{j}_verdict"].isin(["ours", "mo"])] for j in JUDGES}
    bias = {j: (d[f"{j}_verdict"] == d.A).mean() for j, d in dec.items()}
    o = v[v.stratum == "original"].merge(ours.drop_duplicates("doi_r")[["doi_r", "link_method", "link_confidence"]], on="doi_r")
    lost = o[o.consensus == "mo"]
    per = {s: v[v.stratum == s].consensus.value_counts() for s in STRATA}
    n = {s: (v.stratum == s).sum() for s in STRATA}
    return f"""
<h3>What the judges found</h3>
<ul>
<li><b>Where the Observatory and our pipeline disagree, the Observatory is usually right.</b> Both judges side with it on
{per['original'].get('mo', 0)} of {n['original']} different-original items (with us on {per['original'].get('ours', 0)}),
{per['contradiction'].get('mo', 0)} of {n['contradiction']} flat contradictions (with us on {per['contradiction'].get('ours', 0)}), and
{per['cbd'].get('mo', 0)} of {n['cbd']} items where we gave no verdict although the paper reports one.</li>
<li><b>Our wrong originals are sibling references, picked with confidence.</b> Of {len(lost)} lost original items,
{(lost.link_method == 'llm_references').sum()} came from the reference-list pick (<code>llm_references</code>) and
{(lost.link_confidence == 'high').sum()} were at <code>link_confidence</code> high. The judges' reasons repeat one pattern: the pipeline chose
the same authors' related paper, the source of the materials, or a background citation, rather than the study the paper says it re-tests.</li>
<li><b>The success/inconclusive boundary is genuinely contested.</b> On those {n['inconclusive']} items the judges split on
{per['inconclusive'].get('split', 0)}; there, neither database is clearly wrong.</li>
<li><b>Scope of the claim.</b> The sample is drawn from disagreements only, so these rates describe the ~10% of papers where we diverge,
not either database overall. Checks: the judges agree with each other on {(v.consensus != 'split').mean():.0%} of items; when decisive,
they chose the answer shown first {bias['claude']:.0%} (Claude) and {bias['codex']:.0%} (Codex) of the time, so there is no position bias.
Items marked “judges split” need a human.</li>
</ul>"""


def main() -> None:
    ours, mo = load()
    v = verdicts()
    v.to_csv(HERE / "verdicts.csv", index=False, encoding="utf-8-sig")
    wk = pd.read_csv(HERE / "works.csv", dtype=str)
    pr = pd.read_csv(HERE / "pairs.csv", dtype=str)
    mf = pd.read_csv(HERE / "mo_vs_flora.csv")
    comp = pr[(pr.kind == "agree") | pr.kind.str.startswith(("contradiction", "inconclusive", "reversal"))]
    done = {j: (v[f"{j}_verdict"] != "missing").sum() for j in JUDGES}
    mfc = mf[mf.comparable]
    intro = f"""
<h1>FLoRA pipeline × Metascience Observatory — where we disagree, and who is right</h1>
<p class='meta'>Generated from <code>analysis/mo_observatory/adjudication/</code>. Judged: Claude {done['claude']}/{len(v)},
Codex {done['codex']}/{len(v)}.</p>
<h2>1. How often we agree</h2>
<p>Over the <b>{len(wk):,}</b> Observatory replications our pipeline extracted, counted per paper against the
<i>full</i> Observatory file (it stores one row per effect or lab; an earlier comparison kept one arbitrary row per paper):</p>
<table><tr><th>original study</th><th>papers</th><th>share</th></tr>
{''.join(f"<tr><td>{_e(k)}</td><td>{n}</td><td>{n/len(wk):.0%}</td></tr>" for k, n in wk.original.value_counts().items())}
</table>
<p>Of the “different” ones, about a third turn out to be the same paper under two DOIs
(identical titles: <code>10.1037//</code> vs <code>10.1037/</code>, JSTOR duplicates, working paper vs journal version,
journal-issue DOIs) — they are not disagreements and are not judged below.</p>
<p>On the original both name, the outcome agrees on <b>{(comp.kind=='agree').sum():,} of {len(comp):,} ({(comp.kind=='agree').mean():.0%})</b>
where both give one comparable verdict. Not comparable: {(pr.kind=='theirs: several results (per effect/lab)').sum()} originals where the
Observatory records several results (per effect or lab), {(pr.kind=='reproduction (two-axis, unmapped)').sum()} reproductions coded on our two axes,
{pr.kind.str.startswith('ours:').sum()} where we gave no verdict.</p>
<h3>Calibration: the Observatory against FLoRA's human-curated entries</h3>
<p>On the {len(mf):,} replications FLoRA already holds and the Observatory did <i>not</i> import from FLoRA, the Observatory names the same
original for <b>{mf.same_original.mean():.0%}</b> and the same outcome for <b>{mfc.same_outcome.mean():.0%}</b> ({mfc.same_outcome.sum()}/{len(mfc)}).
Outcome disagreement of this size is therefore normal between two careful sources, not a sign of pipeline error; the success/inconclusive
boundary dominates it there as well.</p>
<h2>2. Blinded adjudication of a stratified sample</h2>
<p>{len(v)} divergent works, drawn per stratum. Each judge saw the paper (full text where we had it, otherwise it looked the paper up by DOI)
and the two answers labelled A/B in random order, without knowing which source gave which. For outcome items “right” means
<i>more defensible</i>; “both defensible” is the typical call on a partial replication.</p>
{_tally(v)}
{_findings(v, ours)}
"""
    filters = ("<div class='filters'>Show: <button class='on' onclick=\"f('stratum','',this)\">all</button>"
               + "".join(f"<button onclick=\"f('stratum','{s}',this)\">{_e(l)}</button>" for s, l in STRATA.items())
               + "<button onclick=\"f('cons','split',this)\">judges split</button>"
               + "<button onclick=\"f('cons','ours',this)\">both: FLoRA right</button>"
               + "<button onclick=\"f('cons','mo',this)\">both: Observatory right</button></div>")
    cards = "".join(_card(it, ours, mo) for it in v.sort_values(["stratum", "consensus"]).to_dict("records"))
    page = (f"<!doctype html><html><head><meta charset='utf-8'><title>FLoRA × Observatory adjudication</title>"
            f"<style>{CSS}</style><script>{JS}</script></head><body>{intro}<h2>3. The items</h2>{filters}{cards}</body></html>")
    (HERE / "report.html").write_text(page, encoding="utf-8")
    print(f"report.html: {len(v)} items; claude {done['claude']}, codex {done['codex']}")
    print(v.groupby("stratum")[["claude_verdict", "codex_verdict", "consensus"]].agg(lambda s: s.value_counts().to_dict()).to_string())


if __name__ == "__main__":
    main()
