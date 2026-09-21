"""Render the FLoRA/Observatory scope comparison as one self-contained HTML page.

Reads the offline screen's verdicts (`screened.csv`), the cause split
(`not_in_pool_causes.csv`) and the Observatory's own labels, and writes
`scope_differences.html`: every work our screen discarded, grouped by the reason the
voters gave, plus the works we would have wanted and could not see.

The reason taxonomy is derived from the voters' own reasoning text, not imposed. Four
specific buckets, priority-ordered, and one residual — and the residual is the honest
one: every discard here comes down to "the paper does not state an aim to re-test a
specific earlier finding", so a work lands in `no_stated_aim` unless its reasoning says
something more particular than that.

    .venv/bin/python -m analysis.mo_observatory.build_scope_html
"""

import csv
import html
import re
import sys
from collections import Counter
from pathlib import Path
from typing import Optional

from shared.utils import clean_doi

csv.field_size_limit(10 ** 9)

HERE = Path(__file__).resolve().parent
OUT = HERE / "scope_differences.html"

STEM = re.compile(r"\breplicat|\breproduc|\bre-?analy", re.I)

# Priority-ordered. `title_only` is objective (no abstract reached the screen at all);
# the rest read the voters' reasoning. The residual is `no_stated_aim`.
REASONS: list[tuple[str, str, Optional[str], str]] = [
    ("title_only", "Only a title was available",
     None,
     "No source had an abstract, so the voters judged the title alone. These are the "
     "weakest of the discards: the paper may well state a replication aim in text "
     "nobody could read. Europe PMC, CrossRef, OpenAlex and Scopus were all asked."),
    ("context_not_aim", "Earlier work is context, not the aim",
     r"context rather than|serves as context|rather than a stated aim|as context\b",
     "The paper compares its results to an earlier study, but the comparison is "
     "background rather than the point of the study. This is the closest call in the "
     "set and where a human reviewer is most likely to disagree with the screen."),
    ("extension", "Extends earlier work to something new",
     r"\bextension of|extends? (?:previous|prior|earlier)|new (?:stimulus|context|sample|"
     r"population|setting)|generalis|generaliz",
     "A study that carries an earlier design to a new stimulus, sample or setting "
     "without stating that it is checking the earlier finding. This is exactly the "
     "Observatory's `close extension` class, and the single biggest source of "
     "disagreement between the two databases."),
    ("original", "Presented as original or novel research",
     r"\boriginal (?:research|study|studies|experiment|neuroimaging|genetic|association|"
     r"empirical|data)|\bnovel\b|presents? new |new (?:evidence|experiments|hypothes)",
     "The voters read the abstract as describing new work — a new hypothesis, a new "
     "model, a first report. Genetic association studies dominate: the field treats "
     "each new cohort as a replication sample and writes it up as an original study."),
    ("no_stated_aim", "No stated aim to re-test a specific finding",
     None,
     "The residual, and the definitional core: nothing in the paper says it set out to "
     "check a particular earlier result. Every other bucket is a more specific version "
     "of this one."),
]


def classify(row: dict) -> str:
    if int(row["abstract_chars"] or 0) == 0:
        return "title_only"
    text = row.get("screen_reasoning") or ""
    for key, _label, pattern, _blurb in REASONS:
        if pattern and re.search(pattern, text, re.I):
            return key
    return "no_stated_aim"


def load() -> tuple[list[dict], dict, dict]:
    mo: dict[str, dict] = {}
    source = next(HERE.glob("replications_database_*.csv"), None)
    if source:
        with source.open(newline="", encoding="utf-8") as handle:
            for raw in csv.DictReader(handle):
                doi = clean_doi(str(raw.get("replication_url") or ""))
                if doi.startswith("10."):
                    mo.setdefault(doi, raw)
    cause: dict[str, str] = {}
    with (HERE / "not_in_pool_causes.csv").open(newline="", encoding="utf-8-sig") as handle:
        for raw in csv.DictReader(handle):
            cause[clean_doi(raw["doi_clean"] or raw["doi_r"])] = raw["cause"]
    with (HERE / "screened.csv").open(newline="", encoding="utf-8-sig") as handle:
        rows = list(csv.DictReader(handle))
    return rows, mo, cause


def _reasoning(text: str) -> str:
    """The two voters' reasoning, split onto their own lines."""
    parts = [p.strip() for p in (text or "").split("||") if p.strip()]
    return "<br>".join(html.escape(p) for p in parts) or "<em>none recorded</em>"


def _row_html(row: dict, mo: dict) -> str:
    m = mo.get(row["doi_r"], {})
    labels = " · ".join(x for x in (m.get("replication_type"), m.get("discipline"),
                                    f"MO: {m.get('result')}" if m.get("result") else "") if x)
    chars = int(row["abstract_chars"] or 0)
    read = (f"{chars:,} chars from {html.escape(row['abstract_source'] or '?')}"
            if chars else "title only")
    return f"""      <tr>
        <td class="doi"><a href="https://doi.org/{html.escape(row['doi_r'])}"
            target="_blank" rel="noopener">{html.escape(row['doi_r'])}</a>
          <div class="meta">{html.escape(labels)}</div>
          <div class="meta read">{read}</div></td>
        <td><div class="title">{html.escape(row['title_r'] or '(no title)')}</div>
          <div class="reason">{_reasoning(row.get('screen_reasoning'))}</div></td>
      </tr>"""


def build() -> str:
    rows, mo, cause = load()
    disc = [r for r in rows if r["screen_verdict"] == "discard"]
    proc = [r for r in rows if r["screen_verdict"] == "proceed"]
    for r in disc:
        r["_reason"] = classify(r)
    counts = Counter(r["_reason"] for r in disc)

    # The recall gap: works the gate never saw because OpenAlex held no abstract,
    # which our own screen then passed AND whose recovered abstract says "replication".
    from analysis.mo_observatory.screen_offline import text_for
    oa: dict[str, str] = {}
    with (HERE / "not_in_pool_causes.csv").open(newline="", encoding="utf-8-sig") as handle:
        for raw in csv.DictReader(handle):
            oa[clean_doi(raw["doi_clean"] or raw["doi_r"])] = raw.get("oa_id") or ""
    missed = []
    for r in proc:
        if not cause.get(r["doi_r"], "").startswith("F_"):
            continue
        text, _src = text_for(r["doi_r"], oa.get(r["doi_r"], ""))
        if text and STEM.search(text):
            hit = STEM.search(text)
            start = max(0, hit.start() - 90)
            r["_snippet"] = ("…" if start else "") + text[start:hit.end() + 110] + "…"
            missed.append(r)

    groups = []
    for key, label, _pattern, blurb in REASONS:
        members = sorted((r for r in disc if r["_reason"] == key),
                         key=lambda r: (mo.get(r["doi_r"], {}).get("discipline") or "",
                                        r["doi_r"]))
        if not members:
            continue
        body = "\n".join(_row_html(r, mo) for r in members)
        groups.append(f"""  <details class="group"{' open' if key != 'no_stated_aim' else ''}>
    <summary><span class="count">{len(members)}</span>{html.escape(label)}</summary>
    <p class="blurb">{html.escape(blurb)}</p>
    <table>{body}
    </table>
  </details>""")

    missed_rows = "\n".join(f"""      <tr>
        <td class="doi"><a href="https://doi.org/{html.escape(r['doi_r'])}"
            target="_blank" rel="noopener">{html.escape(r['doi_r'])}</a>
          <div class="meta">{html.escape((mo.get(r['doi_r'], {}).get('discipline') or ''))}</div></td>
        <td><div class="title">{html.escape(r['title_r'] or '')}</div>
          <div class="reason snippet">{html.escape(r.get('_snippet', ''))}</div></td>
      </tr>""" for r in sorted(missed, key=lambda r: r["doi_r"]))

    tally = "\n".join(
        f"      <tr><td>{html.escape(label)}</td><td class='n'>{counts.get(key, 0)}</td>"
        f"<td class='n'>{counts.get(key, 0) / len(disc):.0%}</td></tr>"
        for key, label, _p, _b in REASONS if counts.get(key))

    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>FLoRA vs the Metascience Observatory — where the scopes differ</title>
<style>
  :root {{ color-scheme: light dark;
    --fg: #16181d; --bg: #fbfbfa; --muted: #6b7280; --line: #e3e3e0;
    --card: #fff; --accent: #8a5a2b; }}
  @media (prefers-color-scheme: dark) {{ :root {{
    --fg: #e8e8e6; --bg: #17181a; --muted: #9aa0a6; --line: #2e3033;
    --card: #1e2022; --accent: #d9a066; }} }}
  * {{ box-sizing: border-box; }}
  body {{ margin: 0 auto; max-width: 62rem; padding: 2.5rem 1.25rem 5rem;
    font: 16px/1.6 -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
    color: var(--fg); background: var(--bg); }}
  h1 {{ font-size: 1.7rem; line-height: 1.25; margin: 0 0 .4rem; }}
  h2 {{ font-size: 1.15rem; margin: 2.75rem 0 .5rem;
    padding-top: 1.25rem; border-top: 1px solid var(--line); }}
  .sub {{ color: var(--muted); margin: 0 0 2rem; }}
  .lede {{ background: var(--card); border: 1px solid var(--line);
    border-left: 3px solid var(--accent); border-radius: 6px; padding: 1rem 1.15rem;
    margin: 1.5rem 0; }}
  .lede p {{ margin: .5rem 0; }}
  .lede p:first-child {{ margin-top: 0; }} .lede p:last-child {{ margin-bottom: 0; }}
  table {{ width: 100%; border-collapse: collapse; }}
  td, th {{ text-align: left; vertical-align: top; padding: .6rem .5rem;
    border-top: 1px solid var(--line); }}
  td.n, th.n {{ text-align: right; font-variant-numeric: tabular-nums; }}
  .tally {{ max-width: 34rem; }}
  .doi {{ width: 17rem; }}
  .doi a {{ color: var(--accent); text-decoration: none; font-size: .85rem;
    font-family: ui-monospace, SFMono-Regular, Menlo, monospace; word-break: break-all; }}
  .doi a:hover {{ text-decoration: underline; }}
  .meta {{ color: var(--muted); font-size: .78rem; margin-top: .2rem; }}
  .meta.read {{ font-style: italic; }}
  .title {{ font-weight: 600; margin-bottom: .3rem; }}
  .reason {{ color: var(--muted); font-size: .87rem; }}
  .snippet {{ font-style: italic; }}
  details.group {{ background: var(--card); border: 1px solid var(--line);
    border-radius: 8px; margin: 1rem 0; padding: .25rem 1.1rem 1rem; }}
  summary {{ cursor: pointer; font-weight: 650; font-size: 1.02rem;
    padding: .85rem 0; list-style: none; }}
  summary::-webkit-details-marker {{ display: none; }}
  summary::before {{ content: "▸"; color: var(--muted); margin-right: .55rem;
    display: inline-block; transition: transform .15s; }}
  details[open] > summary::before {{ transform: rotate(90deg); }}
  .count {{ display: inline-block; min-width: 2.6rem; margin-right: .6rem;
    padding: .1rem .45rem; border-radius: 4px; background: var(--accent);
    color: var(--bg); font-variant-numeric: tabular-nums; text-align: center;
    font-size: .85rem; }}
  .blurb {{ color: var(--muted); font-size: .9rem; margin: 0 0 .5rem; }}
  footer {{ color: var(--muted); font-size: .82rem; margin-top: 3rem;
    border-top: 1px solid var(--line); padding-top: 1rem; }}
  code {{ font-size: .85em; background: var(--card); padding: .1em .35em;
    border-radius: 3px; border: 1px solid var(--line); }}
</style></head><body>

<h1>Where FLoRA's scope and the Metascience Observatory's part company</h1>
<p class="sub">{len(disc)} works the Observatory records as replications and our screen
rejects — with the reason, for each one.</p>

<div class="lede">
<p><strong>What was asked.</strong> {len(rows)} Observatory replications that our Stage 1
search gate never admitted were put to the shipped two-voter screen, unchanged
(<code>classify_replication()</code>, <code>deepseek-v4-flash</code> +
<code>gpt-5.4-mini</code>). It said <strong>proceed on {len(proc)}</strong> and
<strong>discard on {len(disc)}</strong>.</p>
<p><strong>Read the rate carefully.</strong> Every work here is one the gate missed, and
the gate looks for a replication word in the title or abstract. So this population is,
by construction, papers that do not announce themselves — not a random sample, and not
FLoRA's general false-positive rate.</p>
<p><strong>The difference in one line.</strong> FLoRA asks what a paper <em>says it is
doing</em>: a replication states an aim to check a specific earlier finding. The
Observatory asks what a study <em>does</em>: any design that re-tests a reported effect
counts, whether or not the authors frame it that way. Neither is wrong; the gap below
follows from the definitions.</p>
</div>

<h2>The {len(disc)} discards, by reason</h2>
<table class="tally">
      <tr><th>Reason</th><th class="n">Works</th><th class="n">Share</th></tr>
{tally}
</table>

{chr(10).join(groups)}

<h2>Not a scope difference: {len(missed)} we would have wanted and could not see</h2>
<div class="lede">
<p>These passed our own screen, and their abstracts say plainly that they are
replications — <em>“we replicate and extend previous work”</em>. We never saw the
sentence: OpenAlex holds no abstract for these records, so the search gate had only a
title to read. Europe PMC supplied the text afterwards.</p>
<p>Nothing about FLoRA's definition needs to change to want these. They are lost to an
abstract-coverage gap, which is a Stage 1 question and fixable.</p>
</div>
<table>
{missed_rows}
</table>

<footer>
Generated by <code>analysis/mo_observatory/build_scope_html.py</code> from
<code>screened.csv</code>. Source: the Metascience Observatory replications database
(<code>delton137/metascience-observatory</code>, 2026-09-04 snapshot). Screen verdicts
are the shipped prompt and voter pair; nothing here was written to the pipeline.
Reason buckets are derived from the voters' own reasoning text, priority-ordered, with
“no stated aim” as the residual.
</footer>
</body></html>"""


def main(argv: Optional[list[str]] = None) -> int:
    OUT.write_text(build(), encoding="utf-8")
    print(f"wrote {OUT} ({OUT.stat().st_size / 1024:.0f} KB)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
