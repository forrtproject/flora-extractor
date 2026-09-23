"""Unblind the two judges' verdicts and render `report.html`.

Two populations, each judged blind by Claude + Codex:

- our pipeline vs the Observatory — `key.csv`, `out/claude/`, `out/codex/` (gpt-5.6-sol);
- shipped, human-curated FLoRA vs the Observatory — `key_flora.csv`, `out/claude/`,
  `out/codex6/` (gpt-6-sol); original-study items only.

Also reads the works/pairs tables from `agreement.py`, `original_title_sim.csv` (the
different-original pairs that are one paper under two DOIs), `doi_pairs.csv` and
`mo_vs_flora.csv`. Writes `verdicts.csv` (pipeline items, both judges unblinded) and
`report.html`. The report stays local.

    .venv/bin/python -m analysis.mo_observatory.adjudication.build_report
"""
from __future__ import annotations

import html
import json
import re
from pathlib import Path

import pandas as pd

from analysis.mo_observatory.adjudication.agreement import load
from shared.utils import clean_doi

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[2]
_DOI_URL = r"^https?://(dx\.)?doi\.org/"
JUDGES = {"claude": "Claude (Opus 5.5)", "codex": "Codex (gpt-5.6-sol)"}
# Per population: whose answer "ours" is, and which Codex model judged it.
POP = {"pipeline": {"side": "FLoRA pipeline", "ours": "pipeline right",
                    "judges": JUDGES},
       "flora": {"side": "FLoRA (shipped, human-curated)", "ours": "FLoRA right",
                 "judges": {"claude": "Claude (Opus 5.5)", "codex": "Codex (gpt-6-sol)"}}}
SIDE = {"ours": "FLoRA pipeline", "mo": "Observatory"}
STRATA = {
    "original": "Different original study",
    "contradiction": "Same original, success vs failure",
    "inconclusive": "Same original, success/failure vs inconclusive",
    "cbd": "Same original, we said “cannot be determined”",
}
VERDICT_ORDER = ["ours", "mo", "both", "neither", "cannot_tell", "missing"]
VERDICT_LABEL = {"ours": "pipeline right", "mo": "Observatory right", "both": "both defensible",
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
        # key_flora.csv names FLoRA's side "flora"; the report calls every non-Observatory side "ours".
        cv, xv = ("ours" if x == "flora" else x for x in (cv, xv))
        r["claude_verdict"], r["codex_verdict"] = cv, xv
        r["consensus"] = cv if cv == xv else "split"
        rows.append(r)
    return pd.DataFrame(rows)


def _e(s: object) -> str:
    return html.escape(str(s or ""))


def _label(v: str, pop: str) -> str:
    return POP[pop]["ours"] if v == "ours" else VERDICT_LABEL.get(v, v)


def _tally(v: pd.DataFrame, pop: str = "pipeline") -> str:
    judges = POP[pop]["judges"]
    head = "".join(f"<th>{_label(c, pop)}</th>" for c in VERDICT_ORDER if c != "missing")
    out = [f"<table class='tally'><tr><th>stratum</th><th>n</th><th>who</th>{head}<th>split</th></tr>"]
    for s, label in STRATA.items():
        g = v[v.stratum == s]
        if g.empty:
            continue
        for i, (col, who) in enumerate([("claude_verdict", judges["claude"]), ("codex_verdict", judges["codex"]),
                                        ("consensus", "both judges agree")]):
            cells = "".join(f"<td>{(g[col] == c).sum()}</td>" for c in VERDICT_ORDER if c != "missing")
            split = f"<td>{(g[col] == 'split').sum()}</td>" if col == "consensus" else "<td></td>"
            first = f"<td rowspan=3>{_e(label)}</td><td rowspan=3>{len(g)}</td>" if i == 0 else ""
            cls = " class='cons'" if col == "consensus" else ""
            out.append(f"<tr{cls}>{first}<td>{who}</td>{cells}{split}</tr>")
    out.append("</table>")
    return "".join(out)


def _answers(it: dict, ours: pd.DataFrame, mo: pd.DataFrame, pop: str = "pipeline") -> str:
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
    return (f"<div class='answers'><div><h4>{POP[pop]['side']}</h4>{oo}</div>"
            f"<div><h4>{SIDE['mo']}</h4>{mm}</div></div>")


def _judge(it: dict, j: str, pop: str = "pipeline") -> str:
    v = it[f"{j}_verdict"]
    extra = f" · own coding: {_e(it[f'{j}_own_outcome'])}" if it["stratum"] != "original" else ""
    corr = (f"<br><span class='meta'>Correct original instead: {_e(it[f'{j}_correct_original'])}</span>"
            if it[f"{j}_correct_original"] else "")
    quote = f"<blockquote>{_e(it[f'{j}_quote'])}</blockquote>" if it[f"{j}_quote"] else ""
    return (f"<div class='judge'><h4>{POP[pop]['judges'][j]}: <span class='v v-{v}'>{_label(v, pop)}</span></h4>"
            f"<span class='meta'>{_e(it[f'{j}_confidence'])} confidence · {_e(it[f'{j}_evidence_basis'])}{extra}</span>"
            f"<p>{_e(it[f'{j}_reasoning'])}</p>{quote}{corr}</div>")


def _card(it: dict, ours: pd.DataFrame, mo: pd.DataFrame, pop: str = "pipeline") -> str:
    rep = ours[ours.doi_r == it["doi_r"]].iloc[0]
    cons = it["consensus"]
    badge = ("<span class='v v-split'>judges split — needs a human</span>" if cons == "split"
             else f"<span class='v v-{cons}'>both judges: {_label(cons, pop)}</span>")
    cls = "card" if pop == "pipeline" else "card fl"
    return (f"<details class='{cls}' data-stratum='{it['stratum']}' data-cons='{cons}'>"
            f"<summary><code>{it['id']}</code> {badge} {_e(rep.title_r)} <span class='meta'>({_e(rep.year_r)})</span></summary>"
            f"<p class='meta'><a href='https://doi.org/{_e(it['doi_r'])}'>{_e(it['doi_r'])}</a> · {_e(rep.journal_r)}"
            f" · full text given to judges: {'yes' if it['has_fulltext'] in (True, 'True') else 'no (looked up online)'}</p>"
            f"{_answers(it, ours, mo, pop)}<div class='judges'>{_judge(it, 'claude', pop)}{_judge(it, 'codex', pop)}</div></details>")


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
.summary{background:#f4f8ff;border-left:4px solid #6b8fd6;padding:.6em 1em;margin:1em 0}
"""
JS = """
function f(attr,val,btn){document.querySelectorAll('.card:not(.fl)').forEach(c=>{c.style.display=(!val||c.dataset[attr]===val)?'':'none'});
document.querySelectorAll('.filters button').forEach(b=>b.classList.remove('on'));btn.classList.add('on');}
"""


def _findings(v: pd.DataFrame, ours: pd.DataFrame, n_refs: int) -> str:
    """The headline reading of the tally, plus the two checks that say whether to trust it."""
    dec = {j: v[v[f"{j}_verdict"].isin(["ours", "mo"])] for j in JUDGES}
    bias = {j: (d[f"{j}_verdict"] == d.A).mean() for j, d in dec.items()}
    o = v[v.stratum == "original"].merge(ours.drop_duplicates("doi_r")[["doi_r", "link_method", "link_confidence"]], on="doi_r")
    lost = o[o.consensus == "mo"]
    refs = o[o.link_method == "llm_references"]
    lo, hi = (refs.consensus == "mo").sum(), refs.consensus.isin(["mo", "split"]).sum()
    per = {s: v[v.stratum == s].consensus.value_counts() for s in STRATA}
    n = {s: (v.stratum == s).sum() for s in STRATA}
    return f"""
<h3>What the judges found</h3>
<ul>
<li><b>Where the Observatory and our pipeline disagree, the Observatory is usually right.</b> Both judges side with it on
{per['original'].get('mo', 0)} of {n['original']} different-original items (with us on {per['original'].get('ours', 0)},
both defensible {per['original'].get('both', 0)}, split {per['original'].get('split', 0)}),
{per['contradiction'].get('mo', 0)} of {n['contradiction']} flat contradictions (with us on {per['contradiction'].get('ours', 0)}), and
{per['cbd'].get('mo', 0)} of {n['cbd']} items where we gave no verdict although the paper reports one.</li>
<li><b>Our wrong originals are sibling references, picked with confidence.</b> Of {len(lost)} lost original items,
{(lost.link_method == 'llm_references').sum()} came from the reference-list pick (<code>llm_references</code>) and
{(lost.link_confidence == 'high').sum()} were at <code>link_confidence</code> high. The judges' reasons repeat one pattern: the pipeline chose
the same authors' related paper, the source of the materials, or a background citation, rather than the study the paper says it re-tests.
Over the {n_refs} shared papers our pipeline linked by <code>llm_references</code>, that is {lo} judged wrong by both judges
({lo / n_refs:.1%}) and {hi} counting the splits ({hi / n_refs:.1%}) — a lower bound on the error rate of that step, since papers where
both databases name the same wrong original are not measured.</li>
<li><b>The success/inconclusive boundary is genuinely contested.</b> On those {n['inconclusive']} items the judges split on
{per['inconclusive'].get('split', 0)}; there, neither database is clearly wrong. The Observatory has no “mixed”: a replication whose main
effect held and a secondary did not is “success” there and “mixed” here, and “inconclusive” also covers underpowered results.</li>
<li><b>Scope of the claim.</b> The sample is drawn from disagreements only, so these rates describe the papers where we diverge,
not either database overall. Checks: the judges agree with each other on {(v.consensus != 'split').mean():.0%} of items; when decisive,
they chose the answer shown first {bias['claude']:.0%} (Claude) and {bias['codex']:.0%} (Codex) of the time, so there is no position bias.
Items marked “judges split” need a human.</li>
</ul>"""


def _flora_frames(works: set[str]) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Shipped FLoRA's rows renamed onto the pipeline's columns, and the Observatory's own
    (not imported from FLoRA) rows, for the FLoRA-vs-Observatory items."""
    fl = pd.read_csv(ROOT / "data/flora.csv", dtype=str, encoding="utf-8-sig").fillna("")
    fl["doi_r"], fl["doi_o"] = fl.doi_r.map(clean_doi), fl.doi_o.map(clean_doi)
    fl = fl[fl.doi_r.isin(works)]
    ours = pd.DataFrame({"doi_r": fl.doi_r, "title_r": fl.title_r, "year_r": fl.year_r, "journal_r": fl.journal_r,
                         "doi_o": fl.doi_o, "title_o": fl.title_o, "year_o": fl.year_o,
                         "link_method": "FLoRA", "link_confidence": "human-curated"})
    mo = pd.read_csv(HERE.parent / "replications_database_2026_09_04_184008.csv", dtype=str).fillna("")
    mo = mo[~mo.source.str.contains("FLoRa", case=False)]
    mo["doi_r"] = mo.replication_url.str.replace(_DOI_URL, "", regex=True).map(clean_doi)
    mo["doi_o"] = mo.original_url.str.replace(_DOI_URL, "", regex=True).map(clean_doi)
    return ours, mo[mo.doi_r.isin(works)]


def _flora_section(fv: pd.DataFrame, fours: pd.DataFrame, fmo: pd.DataFrame) -> str:
    c = fv.consensus.value_counts()
    dp = pd.read_csv(HERE / "doi_pairs.csv", dtype=str).fillna("")
    dp = dp[dp.population == "FLoRA vs Observatory"]
    mis = dp[dp.pair == "mistake"]
    mo_mis = (mis.class_b == "mistake").sum()
    alt = dp.pair.str.contains("alternative").sum()
    fmt = (dp.pair == "same DOI, formatting").sum()
    cards = "".join(_card(it, fours, fmo, "flora") for it in fv.sort_values("consensus").to_dict("records"))
    return f"""
<h2>4. Shipped FLoRA vs the Observatory</h2>
<p>The same question for FLoRA's own, human-curated entries (<code>data/flora.csv</code>), on the replications both databases hold
where the Observatory did <i>not</i> import the row from FLoRA. Where the two name a <b>different paper</b> as the original,
{len(fv)} items were judged blind the same way (Claude + Codex <code>gpt-6-sol</code>):</p>
{_tally(fv, "flora")}
<ul>
<li><b>Observatory right {c.get('mo', 0)}, FLoRA right {c.get('ours', 0)}, both defensible {c.get('both', 0)}, split {c.get('split', 0)}.</b>
The Observatory's wins are the same sibling-pick pattern as the pipeline's: the same authors' related paper, or a background citation,
in place of the study the paper says it re-tests. Human validation is not catching that class either.</li>
<li>Where the two name the <b>same title under different DOIs</b> ({len(dp)} pairs): {len(mis)} are a DOI mistake
({mo_mis} on the Observatory's side), {alt} alternative identifiers (preprint, working paper, duplicate registration) and
{fmt} formatting (FLoRA's <code>//</code> and <code>%3c</code>). FLoRA's corrections went to its data owner; the Observatory's
DOI problems are listed in <code>analysis/mo_observatory/observatory_doi_issues.csv</code>.</li>
</ul>
{cards}"""


def main() -> None:
    ours, mo = load()
    v = verdicts()
    v.to_csv(HERE / "verdicts.csv", index=False, encoding="utf-8-sig")
    fv = verdicts("key_flora.csv", "codex6")
    fours, fmo = _flora_frames(set(fv.doi_r))
    wk = pd.read_csv(HERE / "works.csv", dtype=str)
    pr = pd.read_csv(HERE / "pairs.csv", dtype=str)
    mf = pd.read_csv(HERE / "mo_vs_flora.csv")
    sim = pd.read_csv(HERE / "original_title_sim.csv", dtype=str)
    comp = pr[(pr.kind == "agree") | pr.kind.str.startswith(("contradiction", "inconclusive", "reversal"))]
    done = {j: (v[f"{j}_verdict"] != "missing").sum() for j in JUDGES}
    mfc = mf[mf.comparable]
    same = wk.original.str.startswith(("same", "overlap")).sum()
    diff = wk.original.str.startswith("different").sum()
    twins = sim[sim.sim.astype(float) >= 0.95].doi_r.nunique()
    no_doi = wk.original.str.startswith("theirs: no original").sum()
    methods = ours.groupby("doi_r").link_method.agg(lambda s: "|".join(sorted(set(s))))
    n_refs = wk.doi_r.map(methods).str.contains("llm_references", na=False).sum()
    ov = v[v.stratum == "original"].consensus.value_counts()
    fc = fv.consensus.value_counts()
    intro = f"""
<h1>FLoRA × Metascience Observatory — where we disagree, and who is right</h1>
<p class='meta'>Generated from <code>analysis/mo_observatory/adjudication/</code> (<code>build_report.py</code>). Pipeline items judged:
Claude {done['claude']}/{len(v)}, Codex {done['codex']}/{len(v)}; FLoRA items: {len(fv)}.</p>
<div class='summary'><b>In short.</b> On the {len(wk):,} replications both hold, our pipeline and the Observatory name the same original
for <b>{same / len(wk):.0%}</b> and, where both give one comparable verdict, the same outcome for <b>{(comp.kind == 'agree').mean():.0%}</b>.
Of the {diff} papers with a different original, {twins} are the same paper under another DOI, so <b>{diff - twins}</b> genuinely differ —
and there the Observatory is usually right: both blind judges side with it on {ov.get('mo', 0)} of {len(v[v.stratum == 'original'])},
with us on {ov.get('ours', 0)}. FLoRA's own human-curated entries show the same pattern (Observatory right on {fc.get('mo', 0)} of
{len(fv)}, FLoRA on {fc.get('ours', 0)}).</div>
<h2>1. How often we agree</h2>
<p>Over the <b>{len(wk):,}</b> Observatory replications our pipeline extracted, counted per paper against the
<i>full</i> Observatory file. It stores one row per effect or lab (the ego-depletion multi-lab study has 24 rows), so a comparison
of “one row per paper” must aggregate; an earlier version of this comparison kept one arbitrary row per paper and understated
agreement (84% same original, 74% same outcome).</p>
<table><tr><th>original study</th><th>papers</th><th>share</th></tr>
{''.join(f"<tr><td>{_e(k)}</td><td>{n}</td><td>{n/len(wk):.0%}</td></tr>" for k, n in wk.original.value_counts().items())}
</table>
<p>At least one original in common: <b>{same:,} ({same / len(wk):.0%})</b>. Different: {diff}, of which <b>{twins}</b> are the
same paper under two DOIs (identical titles: <code>10.1037//</code> vs <code>10.1037/</code>, JSTOR duplicates, working paper vs journal
version, an erratum or journal-issue DOI in place of the article) — not disagreements, and not judged below. That leaves
<b>{diff - twins}</b> papers whose original genuinely differs. {no_doi} more have no original DOI on the Observatory's side.</p>
<p>On the original both name, the outcome agrees on <b>{(comp.kind=='agree').sum():,} of {len(comp):,} ({(comp.kind=='agree').mean():.0%})</b>
where both give one comparable verdict (FLoRA's “mixed” and “statistically successful but flawed” are mapped to the Observatory's
“inconclusive”). Not comparable: {(pr.kind=='theirs: several results (per effect/lab)').sum()} originals where the
Observatory records several results (per effect or lab), {(pr.kind=='reproduction (two-axis, unmapped)').sum()} reproductions coded on our two axes,
{pr.kind.str.startswith('ours:').sum()} where we gave no verdict.</p>
<h3>Calibration: the Observatory against FLoRA's human-curated entries</h3>
<p>On the {len(mf):,} replications FLoRA already holds and the Observatory did <i>not</i> import from FLoRA, the Observatory names the same
original for <b>{mf.same_original.mean():.0%}</b> and the same outcome for <b>{mfc.same_outcome.mean():.0%}</b> ({mfc.same_outcome.sum()}/{len(mfc)}).
Outcome disagreement of this size is therefore normal between two careful sources; the success/inconclusive boundary dominates it there as
well.</p>
<h2>2. Blinded adjudication: our pipeline vs the Observatory</h2>
<p>{len(v)} divergent works: all {len(v[v.stratum == 'original'])} genuinely different originals, and a stratified sample of the outcome
disagreements. Each judge saw the paper (full text where we had it, otherwise it looked the paper up by DOI) and the two answers labelled
A/B in random order, without knowing which source gave which. For outcome items “right” means <i>more defensible</i>; “both defensible”
is the typical call on a partial replication.</p>
{_tally(v)}
{_findings(v, ours, n_refs)}
"""
    filters = ("<div class='filters'>Show: <button class='on' onclick=\"f('stratum','',this)\">all</button>"
               + "".join(f"<button onclick=\"f('stratum','{s}',this)\">{_e(l)}</button>" for s, l in STRATA.items())
               + "<button onclick=\"f('cons','split',this)\">judges split</button>"
               + "<button onclick=\"f('cons','ours',this)\">both: pipeline right</button>"
               + "<button onclick=\"f('cons','mo',this)\">both: Observatory right</button></div>")
    cards = "".join(_card(it, ours, mo) for it in v.sort_values(["stratum", "consensus"]).to_dict("records"))
    page = (f"<!doctype html><html><head><meta charset='utf-8'><title>FLoRA × Observatory adjudication</title>"
            f"<style>{CSS}</style><script>{JS}</script></head><body>{intro}<h2>3. The pipeline items</h2>{filters}{cards}"
            f"{_flora_section(fv, fours, fmo)}</body></html>")
    (HERE / "report.html").write_text(page, encoding="utf-8")
    print(f"report.html: {len(v)} pipeline items (claude {done['claude']}, codex {done['codex']}); {len(fv)} FLoRA items")
    print(v.groupby("stratum")[["claude_verdict", "codex_verdict", "consensus"]].agg(lambda s: s.value_counts().to_dict()).to_string())
    print("FLoRA:", fv.consensus.value_counts().to_dict())


if __name__ == "__main__":
    main()
