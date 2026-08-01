"""Assemble the audit page: head + styles + body + prompt appendix + comment layer."""

HEAD = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>FLoRA Extractor — Screening Pipeline Audit &amp; Proposed Fixes</title>
<style>
  :root {
    --ink:#1c2333; --muted:#5b6478; --accent:#1a5fb4; --rule:#e3e6ee; --bg:#fdfdfc;
    --panel:#f4f6fa; --sev-high:#b3261e; --sev-med:#9a6a00; --sev-low:#4a5568;
    --fix-bg:#eef6ee; --fix-border:#3a7d44; --code-bg:#f0f2f7;
    --corr-bg:#fdf6e8; --corr-border:#c98a1b;
  }
  * { box-sizing:border-box; }
  body { margin:0; background:var(--bg); color:var(--ink);
    font:16px/1.65 "Avenir Next","Segoe UI",system-ui,sans-serif; }
  main { max-width:880px; margin:0 auto; padding:3rem 1.5rem 6rem; }
  h1 { font-size:1.9rem; line-height:1.25; margin:0 0 .3rem; }
  .subtitle { color:var(--muted); margin:0 0 2.2rem; font-size:1.02rem; }
  h2 { font-size:1.35rem; margin:3rem 0 .8rem; padding-top:1.2rem; border-top:2px solid var(--rule); }
  h3 { font-size:1.08rem; margin:1.8rem 0 .5rem; }
  h4 { font-size:.95rem; margin:1.2rem 0 .4rem; color:var(--muted); }
  p { margin:.6rem 0; }
  code { background:var(--code-bg); padding:.1em .35em; border-radius:4px; font-size:.88em;
    font-family:"SF Mono",ui-monospace,Menlo,Consolas,monospace; }
  pre { background:var(--code-bg); padding:.9rem 1rem; border-radius:8px; overflow-x:auto;
    font-size:.84rem; line-height:1.5; }
  pre code { background:none; padding:0; }
  table { border-collapse:collapse; width:100%; margin:1rem 0; font-size:.92rem; }
  th,td { border:1px solid var(--rule); padding:.45rem .6rem; text-align:left; vertical-align:top; }
  th { background:var(--panel); font-weight:600; }
  .tablewrap { overflow-x:auto; }
  .tldr { background:var(--panel); border-left:4px solid var(--accent); padding:1rem 1.2rem;
    border-radius:0 8px 8px 0; margin:1.5rem 0 2rem; }
  .tldr p:first-child { margin-top:0; }
  .finding { border:1px solid var(--rule); border-radius:10px; padding:1rem 1.2rem 1.1rem;
    margin:1.4rem 0; background:#fff; }
  .finding h3 { margin:0 0 .4rem; }
  .sev { display:inline-block; font-size:.72rem; font-weight:700; letter-spacing:.04em;
    padding:.12em .55em; border-radius:999px; vertical-align:middle; margin-left:.5rem;
    text-transform:uppercase; color:#fff; }
  .sev-high { background:var(--sev-high); }
  .sev-med { background:var(--sev-med); }
  .sev-low { background:var(--sev-low); }
  .verified { display:inline-block; font-size:.72rem; font-weight:600; padding:.12em .55em;
    border-radius:999px; margin-left:.4rem; background:#e2ecf9; color:#1a5fb4; vertical-align:middle; }
  .fix { background:var(--fix-bg); border-left:4px solid var(--fix-border); padding:.7rem 1rem;
    border-radius:0 8px 8px 0; margin-top:.8rem; }
  .fix strong:first-child { color:var(--fix-border); }
  .correction { background:var(--corr-bg); border-left:4px solid var(--corr-border);
    padding:.6rem .9rem; border-radius:0 6px 6px 0; margin:.8rem 0; font-size:.94rem; }
  .loc { color:var(--muted); font-size:.85rem; }
  ol li, ul li { margin:.35rem 0; }
  .ladder td:first-child { white-space:nowrap; font-weight:600; }
  .pathnote { color:var(--muted); font-size:.9rem; font-style:italic; }
  .toc { background:var(--panel); border-radius:10px; padding:1rem 1.4rem; }
  .toc ol { margin:.3rem 0; padding-left:1.2rem; }
  a { color:var(--accent); }
  .stat { font-variant-numeric:tabular-nums; }
  .callid { display:inline-block; background:var(--accent); color:#fff; font-weight:700;
    font-size:.78rem; padding:.1em .5em; border-radius:5px; letter-spacing:.02em;
    font-family:"SF Mono",ui-monospace,Menlo,monospace; }
  a.callref { text-decoration:none; font-weight:700; font-size:.82rem; padding:.08em .42em;
    border-radius:5px; background:#e2ecf9; color:#12457f; border:1px solid #c4d8f0;
    font-family:"SF Mono",ui-monospace,Menlo,monospace; white-space:nowrap; }
  a.callref:hover { background:var(--accent); color:#fff; }
  .warn { display:block; color:var(--sev-med); font-size:.85rem; margin-top:.2rem; }
  section.prompt { border:1px solid var(--rule); border-radius:10px; padding:1rem 1.2rem;
    margin:1.4rem 0; background:#fff; }
  section.prompt h3 { margin:0 0 .6rem; }
  section.prompt ul.meta { font-size:.9rem; color:var(--muted); padding-left:1.1rem; margin:.4rem 0 .8rem; }
  section.prompt ul.meta code { font-size:.85em; }
  pre.prompt-text { background:#fbfaf7; border:1px solid #e8e4da; white-space:pre-wrap;
    word-wrap:break-word; font-size:.82rem; }
  footer { margin-top:4rem; color:var(--muted); font-size:.85rem; border-top:1px solid var(--rule);
    padding-top:1rem; }
  @media (max-width:600px) { main { padding:2rem 1rem 4rem; } h1 { font-size:1.5rem; } }
</style>
</head>
<body>
<main>
"""

FOOT = """
<footer>
Audit conducted 2026-07-28 with parallel review agents plus direct verification of every
severity-high finding and every quantitative claim against code and on-disk data (489 refscreen
caches, 601 match-type caches, extracted.csv, extracted-test.csv, not_a_replication.csv,
screen_disagreement.csv, doi_audit_report.csv). Amber blocks mark claims that changed or did not
survive checking. Comment layer: select any text → 💬 Comment / ✏️ Suggest.
</footer>
</main>

<script>
(function () {
  window.HC_CONFIG = {
    endpoint: 'https://script.google.com/macros/s/AKfycbyx858xfSCgHQ8RmbfbMsdzcmzrkNs_JZVmqeEBVrH-S6g8YJbI32CY7if-k8oTzg-eyQ/exec',
    project: 'flora-screening-audit'
  };
  var BASE = 'https://html-comments.surge.sh/';
  var link = document.createElement('link');
  link.rel = 'stylesheet'; link.href = BASE + 'html-comments.css';
  document.head.appendChild(link);
  var s = document.createElement('script');
  s.src = BASE + 'html-comments.js';
  document.head.appendChild(s);
})();
</script>
</body>
</html>
"""

body = open("audit_body.html").read()
prompts = open("prompts_fragment.html").read()
assert "<!--PROMPTS-->" in body, "placeholder missing"
body = body.replace("<!--PROMPTS-->", prompts)

disposition = open("disposition_fragment.html").read()

out = HEAD + body + disposition + FOOT
open("../screening_audit.html", "w").write(out)
print(f"wrote ../screening_audit.html ({len(out):,} bytes)")

# sanity: every callref target exists as an anchor id
import re
refs = set(re.findall(r'href="#(L\d+|S\d+|F\d+)"', out))
ids = set(re.findall(r'<a id="(L\d+|S\d+|F\d+)"></a>', out))
missing = sorted(refs - ids)
print("callrefs:", len(refs), "| anchors:", len(ids),
      "| BROKEN:", missing if missing else "none")
