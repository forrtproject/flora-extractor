// stage_common.js — what every per-stage page shares: formatting, the provenance
// line, the pipeline map, the open-issues panel, and contents highlighting.
//
// Loaded before the page's own script (plain scripts, `defer`, so order holds and
// these are ordinary globals). Extracted when Stage 2 arrived and the alternative
// was a second copy of ~200 lines that would drift from the first.
//
// The rule these helpers exist to keep: a number the page could not read renders as
// its reason and the command that supplies it, never as zero.
const $ = (id) => document.getElementById(id);
const esc = (s) => String(s ?? "").replace(/[&<>"]/g,
  (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
const n = (v) => Number(v ?? 0).toLocaleString();
const pct = (part, whole) => (whole ? `${(100 * part / whole).toFixed(1)}%` : "—");

function bytes(b) {
  if (!b) return null;
  const u = ["B", "KB", "MB", "GB", "TB"];
  let i = 0, v = Number(b);
  while (v >= 1024 && i < u.length - 1) { v /= 1024; i += 1; }
  return `${v.toFixed(v >= 100 || i === 0 ? 0 : 1)} ${u[i]}`;
}

async function getJSON(url) {
  const res = await fetch(url);
  if (!res.ok) throw new Error(`${url} → ${res.status}`);
  return res.json();
}

function fail(el, err) {
  if (!el) return;
  el.className = "failed";
  el.textContent = `Could not read this: ${err.message}`;
}

// A provenance object as the one line that says whether to trust the number above it.
function provLine(p) {
  if (!p) return "";
  if (p.state === "absent") {
    return `<div class="note">Not available here — ${esc(p.reason || "no reason given")}</div>`;
  }
  const bits = [`read from <b>${esc(p.source)}</b>`, esc(p.state)];
  if (p.as_of) bits.push(`as of ${esc(p.as_of)}`);
  if (p.machine) bits.push(`written on <b>${esc(p.machine)}</b>'s machine`);
  return `<div class="note ok">${bits.join(" · ")}</div>`;
}

// ── the pipeline map ────────────────────────────────────────────────────────
// Bars are log-scaled: linear, everything past the pool is a hairline and the page
// would read as "nothing narrows after Stage 1", which is the reverse of the truth.
function bar(count, max) {
  // Clamped at both ends: a caller that knows no corpus size passes a max smaller than
  // some node's count, and an unclamped log ratio then renders a 2,200%-wide bar that
  // runs off the card. A bar can never mean more than "all of it".
  if (!count || !max || max < 2) return `<div class="mapbar"></div>`;
  const w = Math.min(100, Math.max(2, 100 * Math.log10(count + 1) / Math.log10(max + 1)));
  return `<div class="mapbar"><span style="width:${w.toFixed(1)}%"></span></div>`;
}

function mapNode({ name, count, note, source, here, missing }, max, hereLabel) {
  const value = count == null
    ? `<span class="mapcount none">${esc(missing || "not on this machine")}</span>`
    : `<span class="mapcount">${n(count)}</span>`;
  return `<div class="mapnode${here ? " here" : ""}"${
    here ? ` data-here="${esc(hereLabel)}"` : ""}>
    <span class="mapname">${esc(name)}</span>${value}
    ${note ? `<p class="mapnote">${note}</p>` : ""}
    ${bar(count, max)}
    ${source ? `<span class="mapsrc">${esc(source)}</span>` : ""}
  </div>`;
}

const mapEdge = (label, detail) => `<div class="mapedge">
  <span class="arrow">↓</span><span><b>${esc(label)}</b>${
    detail ? ` — ${detail}` : ""}</span></div>`;

// The whole pipeline, one node per hand-off. *hereIds* are the nodes belonging to the
// stage whose page this is; they get the outline and the badge. Counts come from the
// shared flow API plus the pool totals the caller already fetched.
async function renderPipelineMap(elId, { snapshot, poolTotals, hereIds, hereLabel }) {
  const el = $(elId);
  if (!el) return;
  try {
    const flow = await getJSON("/api/dashboard/flow").catch(() => ({ stages: [] }));
    const byId = Object.fromEntries((flow.stages || []).map((s) => [s.id, s]));
    const snap = snapshot || {};
    const totals = poolTotals || null;
    const counts = [snap.records, totals && totals.total,
                    byId.release_piles && byId.release_piles.count,
                    byId.screened && byId.screened.count,
                    byId.rendered && byId.rendered.count].filter(Boolean);
    const max = snap.records || (totals && totals.total) ||
                (counts.length ? Math.max(...counts) : 0);
    const here = new Set(hereIds || []);
    const node = (id, spec) => mapNode({ ...spec, here: here.has(id) }, max, hereLabel);

    el.className = "map";
    el.innerHTML = [
      node("snapshot", {
        name: "OpenAlex snapshot",
        count: snap.records,
        missing: "manifest not on this machine",
        note: `Bulk parquet on S3, read once. <code>${esc(snap.base_url || "")}</code>` +
              (snap.files ? ` · ${n(snap.files)} partitions` : "") +
              (snap.bytes ? ` · ${bytes(snap.bytes)}` : ""),
        source: "openalex s3",
      }),
      mapEdge("the search gate",
        "a replication stem in the title or the raw abstract index, <em>or</em> a " +
        "replication concept — unioned, and the only decision Stage 1 makes"),
      node("pool", {
        name: "Survivor pool",
        count: totals && totals.total,
        // Only what this caller actually supplied: a page holding the row count but
        // not the file layout must not render "0 parquet files · null".
        note: [
          totals && totals.files ? `${n(totals.files)} parquet files` : "",
          totals && totals.bytes ? bytes(totals.bytes) : "",
          totals && totals.pool_dir ? `<code>${esc(totals.pool_dir)}</code>` : "",
        ].filter(Boolean).join(" · "),
        source: "survivor pool",
      }),
      mapEdge("routing", "the JSON rule book sorts every row into one pile; " +
        "the text overlay fills abstracts the snapshot did not ship"),
      node("routed", {
        name: "Routed works",
        count: byId.release_piles && byId.release_piles.count,
        missing: "no routing store here",
        note: "Every pool row assigned to exactly one pile — <code>discard</code>, " +
              "<code>pending</code> or a screening pile.",
        source: "routing store",
      }),
      mapEdge("the screen", "two LLM voters; rules may only route or discard, " +
        "so this is the only step that can admit"),
      node("screened", {
        name: "Admitted for screening",
        count: byId.screened && byId.screened.count,
        missing: "no routing store here",
        note: "What Stage 3 is allowed to spend money on.",
        source: "routing store",
      }),
      mapEdge("the resolution ladder",
        "find the original study, code the outcome from the same reading"),
      node("rendered", {
        name: "Rendered rows",
        count: byId.rendered && byId.rendered.count,
        missing: "not rendered here",
        note: "One row per replication–original pair. <code>data/extracted.csv</code> " +
              "— where this repository stops writing.",
        source: "extracted.csv",
      }),
      mapEdge("csv_to_db.py", "in the separate <code>flora-validation</code> repo"),
      node("validated", {
        name: "Supabase — validated",
        count: null,
        missing: "another repository owns this",
        note: "Human validation. This dashboard only <em>reads</em> those tables.",
        source: "supabase",
      }),
    ].join("") +
      `<p class="mapscale">Bars are log-scaled — on a linear scale every step after
       the pool would be invisible.</p>`;
  } catch (err) { fail(el, err); }
}

// ── open issues for this stage ──────────────────────────────────────────────
// Fetched in the BROWSER, straight from the public API, so the Flask app keeps its
// property of doing no network on a request path — and so a checkout with no GitHub
// reachability still serves the whole page, minus this panel.
//
// The selector is a LABEL, not a title convention. Most issues carry no stage prefix,
// so matching on titles would quietly mislabel them — the failure this dashboard
// exists to prevent. One label per stage is the mechanism GitHub already provides.
const REPO = "forrtproject/flora-extractor";

function issuesEmpty(message, hint) {
  return `<p class="issues-none">${esc(message)}${
    hint ? `<br><span class="issues-hint">${hint}</span>` : ""}</p>`;
}

async function renderIssues(label, elId = "issues-body") {
  const el = $(elId);
  if (!el) return;
  const url = `https://api.github.com/repos/${REPO}/issues` +
              `?state=open&labels=${encodeURIComponent(label)}&per_page=100`;
  let res;
  try {
    res = await fetch(url, { headers: { Accept: "application/vnd.github+json" } });
  } catch {
    el.className = "";
    // Offline is not "no open issues". Say which it is.
    el.innerHTML = issuesEmpty("GitHub unreachable from this machine.");
    return;
  }
  el.className = "";
  if (res.status === 403 || res.status === 429) {
    el.innerHTML = issuesEmpty("GitHub rate limit reached.",
      "60 unauthenticated requests an hour, shared by this IP. Try again later.");
    return;
  }
  if (!res.ok) {
    el.innerHTML = issuesEmpty(`GitHub answered ${res.status}.`);
    return;
  }
  // The issues endpoint returns pull requests too; only an issue has no `pull_request`.
  const issues = (await res.json()).filter((i) => !i.pull_request);
  if (!issues.length) {
    el.innerHTML = issuesEmpty(`Nothing carries the ${label} label yet.`,
      `<a href="https://github.com/${REPO}/labels" target="_blank"
        rel="noopener">Label an issue</a> and it appears here.`);
    return;
  }
  el.innerHTML = `<ul class="issues">${issues.map((i) => `
    <li><a href="${esc(i.html_url)}" target="_blank" rel="noopener"
        title="${esc(i.title)}">
      <span class="num">#${i.number}</span>
      <span class="ttl">${esc(i.title)}</span></a></li>`).join("")}</ul>
    <p class="issues-count">${issues.length} open · newest first</p>`;
}

// ── contents highlighting ───────────────────────────────────────────────────
function trackSections() {
  const links = [...document.querySelectorAll(".toc ol a")];
  const byId = new Map(links.map((a) => [a.getAttribute("href").slice(1), a]));
  const seen = new Set();
  const observer = new IntersectionObserver((entries) => {
    entries.forEach((e) => e.isIntersecting ? seen.add(e.target.id)
                                            : seen.delete(e.target.id));
    const first = [...byId.keys()].find((id) => seen.has(id));
    links.forEach((a) =>
      a.classList.toggle("current", a.getAttribute("href").slice(1) === first));
  }, { rootMargin: "-10% 0px -70% 0px" });
  byId.forEach((_, id) => {
    const section = document.getElementById(id);
    if (section) observer.observe(section);
  });
}

// ── prompts ─────────────────────────────────────────────────────────────────
// The exact text a stage sends, expandable like the rule book. Read from
// /api/docs/prompts, which reads `shared/prompts.py` — so a prompt edit moves this
// page and the cache key together and they cannot disagree about what a prompt is.
//
// A `build_*` prompt is assembled at call time from spliced fragments, so its source
// is shown rather than a rendered string: the source IS the prompt, and it is what
// `prompt_version` hashes. *names* picks this stage's prompts out of the full set.
async function renderPrompts(elId, names, notes = {}) {
  const el = $(elId);
  if (!el) return;
  try {
    const d = await getJSON("/api/docs/prompts");
    const wanted = new Set(names);
    const list = (d.prompts || []).filter((p) => wanted.has(p.name));
    el.className = "";
    if (!list.length) {
      el.innerHTML = `<div class="note">None of this stage's prompts were found in
        <code>shared/prompts.py</code>.</div>`;
      return;
    }
    // Named order, not alphabetical: the list reads as the sequence a row meets.
    list.sort((a, b) => names.indexOf(a.name) - names.indexOf(b.name));
    el.innerHTML = list.map((p) => `
      <details class="prompt">
        <summary>
          <span class="pname">${esc(p.name)}</span>
          <span class="pmeta">${esc(p.kind)}${p.fragments.length
            ? ` · ${p.fragments.length} fragment${p.fragments.length === 1 ? "" : "s"}`
            : ""}</span>
          <span class="pmeta">v${esc(String(p.version).slice(0, 10))} · ${
            n(p.lines)} lines</span>
        </summary>
        <div class="prompt-detail">
          ${notes[p.name] ? `<p class="prompt-note">${notes[p.name]}</p>` : ""}
          <p class="frag-label">${p.kind === "builder" ? "assembly" : "prompt text"}</p>
          <pre>${esc(p.body)}</pre>
          ${p.fragments.map((f) => `
            <p class="frag-label">${esc(f.name)} — ${n(f.lines)} lines</p>
            <pre>${esc(f.text)}</pre>`).join("")}
        </div>
      </details>`).join("") +
      `<div class="note">The version is the hash of the prompt text plus every spliced
       fragment — it keys the cache, so editing a prompt invalidates exactly its own
       answers and nothing else. Where the prompt is in a generation fingerprint,
       editing it also reopens every work that prompt decided.</div>`;
  } catch (err) { fail(el, err); }
}
