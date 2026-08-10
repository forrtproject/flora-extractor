"""Generate a self-contained keyboard-driven coding app from the 30-case v3 sheet.

Same conventions as scratch_build_coding_app.py: cases embedded as a JS const,
progress autosaved to localStorage, CSV download. Blind by construction — only
case_id, doi_r, title and abstract are embedded; the key file is never read.
"""
import json

import pandas as pd

df = pd.read_csv("coding_sheet_v3.csv").fillna("")
cases = [{
    "case": str(r["case_id"]),
    "doi": str(r["doi_r"]),
    "title": str(r["title"]),
    "abstract": str(r["abstract"]),
} for _, r in df.iterrows()]

HTML = """<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>FLoRA v3 screen — 30-case discard review</title>
<style>
 :root{--ink:#1c2333;--muted:#5b6478;--rule:#e3e6ee;--bg:#fdfdfc;--panel:#f4f6fa;
       --yes:#2f7d4f;--rep:#1f6f6f;--no:#b3261e;--unc:#9a6a00;--accent:#1a5fb4;--code:#f0f2f7}
 *{box-sizing:border-box}
 body{margin:0;background:var(--bg);color:var(--ink);
      font:16px/1.6 "Avenir Next","Segoe UI",system-ui,sans-serif}
 header{position:sticky;top:0;background:var(--bg);border-bottom:1px solid var(--rule);
        padding:.7rem 1.2rem;display:flex;gap:1rem;align-items:center;flex-wrap:wrap;z-index:10}
 .bar{flex:1;height:8px;background:var(--rule);border-radius:99px;overflow:hidden;min-width:120px}
 .bar>i{display:block;height:100%;background:var(--accent);width:0}
 main{max-width:820px;margin:0 auto;padding:1.4rem 1.2rem 7rem}
 .help{background:var(--panel);border-left:4px solid var(--accent);padding:.6rem .9rem;
       border-radius:0 8px 8px 0;font-size:.92rem;margin:1rem 0}
 .help p{margin:.35rem 0}
 details{margin-top:.6rem}
 summary{cursor:pointer;font-weight:600;font-size:.88rem}
 details ul{margin:.5rem 0 .2rem;padding-left:1.1rem;font-size:.88rem}
 details li{margin:.25rem 0}
 h2{font-size:1.15rem;line-height:1.35;margin:1.2rem 0 .3rem}
 .doi{font-size:.82rem;color:var(--muted);font-family:ui-monospace,Menlo,monospace}
 .abs{white-space:pre-wrap;background:#fff;border:1px solid var(--rule);border-radius:10px;
      padding:.9rem 1.1rem;margin:.9rem 0;font-size:.95rem}
 .row{display:flex;gap:.5rem;flex-wrap:wrap;margin:.9rem 0}
 button{font:inherit;padding:.55rem 1rem;border-radius:9px;border:1px solid var(--rule);
        background:#fff;cursor:pointer}
 button:hover{border-color:var(--accent)}
 button.sel{color:#fff;border-color:transparent}
 #vy.sel{background:var(--yes)} #vr.sel{background:var(--rep)}
 #vn.sel{background:var(--no)} #vu.sel{background:var(--unc)}
 .conf button.sel{background:var(--accent)}
 kbd{font:inherit;font-size:.72rem;background:var(--code);border:1px solid var(--rule);
     border-radius:4px;padding:0 .3em;color:var(--muted)}
 textarea{width:100%;min-height:3.2rem;font:inherit;padding:.5rem .7rem;border-radius:9px;
          border:1px solid var(--rule);resize:vertical}
 footer{position:fixed;bottom:0;left:0;right:0;background:var(--bg);
        border-top:1px solid var(--rule);padding:.6rem 1.2rem;display:flex;gap:.6rem;
        align-items:center;flex-wrap:wrap}
 .sp{flex:1}
 .muted{color:var(--muted);font-size:.85rem}
 .done{text-align:center;padding:3rem 1rem}
 table{border-collapse:collapse;width:100%;font-size:.9rem;margin:1rem 0}
 th,td{border:1px solid var(--rule);padding:.4rem .6rem;text-align:left}
 th{background:var(--panel)}
 @media(max-width:600px){main{padding:1rem .8rem 8rem}}
</style></head><body>
<header>
  <strong id="pos">1 / 30</strong>
  <div class="bar"><i id="fill"></i></div>
  <span class="muted" id="savedmsg">autosaved</span>
</header>
<main id="app"></main>
<footer>
  <button id="prev">← back <kbd>&larr;</kbd></button>
  <button id="skip">skip <kbd>s</kbd></button>
  <span class="sp"></span>
  <span class="muted" id="count"></span>
  <button id="dl">download CSV</button>
  <button id="reset" title="clear all your answers">reset</button>
</footer>
<script>
const CASES = __CASES__;
const HELP = __HELP__;
const KEY = "flora-coding-v3";
let store = {};
try { store = JSON.parse(localStorage.getItem(KEY) || "{}"); } catch(e) { store = {}; }
let i = 0;
const firstUnanswered = () => {
  const k = CASES.findIndex(c => !store[c.case] || !store[c.case].verdict);
  return k < 0 ? CASES.length - 1 : k;
};
i = firstUnanswered();

const save = () => {
  localStorage.setItem(KEY, JSON.stringify(store));
  const m = document.getElementById("savedmsg");
  m.textContent = "saved " + new Date().toLocaleTimeString();
};
const rec = c => (store[c.case] = store[c.case] || {verdict:"", conf:"high", note:""});

function render(){
  const c = CASES[i], r = rec(c);
  document.getElementById("pos").textContent = (i+1) + " / " + CASES.length;
  const answered = CASES.filter(x => store[x.case] && store[x.case].verdict).length;
  document.getElementById("fill").style.width = (100*answered/CASES.length) + "%";
  document.getElementById("count").textContent = answered + " of " + CASES.length + " coded";
  document.getElementById("app").innerHTML = `
    <div class="help">${HELP}</div>
    <h2>${c.title ? esc(c.title) : "(no title in source record)"}</h2>
    ${c.doi ? `<div class="doi">${esc(c.doi)}</div>` : ""}
    <div class="abs">${esc(c.abstract)}</div>
    <div class="row">
      <button id="vy" class="${r.verdict==="yes_replication"?"sel":""}">yes — replication <kbd>y</kbd></button>
      <button id="vr" class="${r.verdict==="yes_reproduction"?"sel":""}">yes — reproduction <kbd>r</kbd></button>
      <button id="vn" class="${r.verdict==="no"?"sel":""}">no <kbd>n</kbd></button>
      <button id="vu" class="${r.verdict==="unclear"?"sel":""}">unclear <kbd>u</kbd></button>
    </div>
    <div class="row conf">
      <span class="muted" style="align-self:center">confidence</span>
      ${["high","medium","low"].map((x,k)=>
        `<button data-c="${x}" class="${r.conf===x?"sel":""}">${x} <kbd>${k+1}</kbd></button>`).join("")}
    </div>
    <textarea id="note" placeholder="note (optional) — especially useful when you disagree with the screen">${esc(r.note)}</textarea>
    <div class="row"><button id="next">next → <kbd>Enter</kbd></button></div>`;
  const set = v => { r.verdict = v; save(); render(); };
  document.getElementById("vy").onclick = () => set("yes_replication");
  document.getElementById("vr").onclick = () => set("yes_reproduction");
  document.getElementById("vn").onclick = () => set("no");
  document.getElementById("vu").onclick = () => set("unclear");
  document.querySelectorAll(".conf button").forEach(b =>
    b.onclick = () => { r.conf = b.dataset.c; save(); render(); });
  const nt = document.getElementById("note");
  nt.oninput = () => { r.note = nt.value; save(); };
  document.getElementById("next").onclick = next;
}
function esc(s){ const d=document.createElement("div"); d.textContent=s==null?"":s; return d.innerHTML; }
function next(){ if(i < CASES.length-1){ i++; render(); } else { finish(); } }
function prev(){ if(i > 0){ i--; render(); } }

function finish(){
  const answered = CASES.filter(x => store[x.case] && store[x.case].verdict);
  const tally = {};
  CASES.forEach(c => {
    const v = (store[c.case] && store[c.case].verdict) || "(not coded)";
    tally[v] = (tally[v] || 0) + 1;
  });
  document.getElementById("app").innerHTML = `<div class="done">
    <h2>${answered.length} of ${CASES.length} coded</h2>
    <table><tr><th>verdict</th><th>n</th></tr>${
      Object.entries(tally).map(([v,n]) =>
        `<tr><td>${v.replace(/_/g," ")}</td><td>${n}</td></tr>`).join("")}
    </table>
    <p class="muted">Download the CSV and hand it back for scoring. Your answers stay in this
    browser until you do.</p>
    <div class="row" style="justify-content:center">
      <button onclick="i=0;render()">review from the start</button>
      <button onclick="dl()">download CSV</button>
    </div></div>`;
}

function dl(){
  const hdr = ["case_id","doi_r","your_verdict","your_confidence","your_note"];
  const q = s => '"' + String(s==null?"":s).replace(/"/g,'""') + '"';
  const lines = [hdr.join(",")].concat(CASES.map(c => {
    const r = store[c.case] || {};
    return [c.case,c.doi,r.verdict||"",r.conf||"",r.note||""].map(q).join(",");
  }));
  const blob = new Blob(["\\ufeff" + lines.join("\\n")], {type:"text/csv;charset=utf-8"});
  const a = document.createElement("a");
  a.href = URL.createObjectURL(blob);
  a.download = "flora_coding_v3_results.csv";
  a.click();
}

document.getElementById("prev").onclick = prev;
document.getElementById("skip").onclick = next;
document.getElementById("dl").onclick = dl;
document.getElementById("reset").onclick = () => {
  if (confirm("Clear all your answers?")) { store = {}; save(); i = 0; render(); }
};
document.addEventListener("keydown", e => {
  if (e.target.tagName === "TEXTAREA") { if (e.key === "Escape") e.target.blur(); return; }
  const c = CASES[i], r = rec(c);
  const k = e.key.toLowerCase();
  if (k === "y") { r.verdict = "yes_replication"; save(); render(); }
  else if (k === "r") { r.verdict = "yes_reproduction"; save(); render(); }
  else if (k === "n") { r.verdict = "no"; save(); render(); }
  else if (k === "u") { r.verdict = "unclear"; save(); render(); }
  else if (k === "1") { r.conf = "high"; save(); render(); }
  else if (k === "2") { r.conf = "medium"; save(); render(); }
  else if (k === "3") { r.conf = "low"; save(); render(); }
  else if (k === "s" || e.key === "ArrowRight") next();
  else if (e.key === "ArrowLeft") prev();
  else if (e.key === "Enter") next();
  else return;
  e.preventDefault();
});
render();
</script></body></html>
"""

HELP = """<p><strong>Every paper below was DISCARDED by the screening step as
&ldquo;not a replication or reproduction&rdquo;.</strong> Judge from the title and
abstract alone whether that discard was right.</p>
<p><strong>replication</strong> = the paper sets out to collect <em>new data</em> to
re-test a finding from an earlier published study.
<strong>reproduction</strong> = it sets out to <em>re-analyse an earlier study's data</em>
to check the reported result.</p>
<details><summary>coding rules (click to expand)</summary><ul>
<li>Checking a specific earlier finding must be a <strong>stated aim</strong> of the paper,
not something that merely happens along the way.</li>
<li>A new context, language, population or setting <strong>still counts</strong>.</li>
<li><strong>Conceptual</strong> replications count &mdash; different methods testing the same claim.</li>
<li>Re-validating an <strong>already published instrument</strong> counts; only the
<em>first</em> validation of a brand-new instrument does not.</li>
<li>Re-testing the authors' <strong>own earlier published finding</strong> counts.</li>
<li>Comments or letters that carry <strong>their own re-analysis</strong> count as reproductions.</li>
<li>Does <strong>not</strong> count: incidental agreement with prior work; testing
established background knowledge; benchmarking a tool or model; &ldquo;replication&rdquo; in the
DNA/biological sense; papers <em>about</em> replication as a topic.</li>
<li>Does <strong>not</strong> count: internal replication, i.e. Study 2 replicating Study 1
inside the same paper.</li>
</ul></details>"""

out = (HTML.replace("__CASES__", json.dumps(cases, ensure_ascii=False))
           .replace("__HELP__", json.dumps(HELP, ensure_ascii=False))
           .replace("1 / 30", f"1 / {len(cases)}"))
open("../coding_app_30_discards.html", "w", encoding="utf-8").write(out)
print(f"{len(cases)} cases -> ../coding_app_30_discards.html ({len(out):,} bytes)")
