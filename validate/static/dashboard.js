// dashboard.js — renders the three bands.
//
// Two rules this file follows:
//   * Absence is never a zero. "No routing store here" and "zero works in this pile"
//     are different facts, so an absent source shows its reason and the command.
//   * Every count that comes from a column is clickable. A number saying something
//     failed is only actionable once you can see WHICH rows it names.
const $ = (id) => document.getElementById(id);
const num = (n) => (n === null || n === undefined ? "—" : Number(n).toLocaleString());
const esc = (s) => String(s ?? "").replace(/[&<>"]/g,
  (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
const pct = (n, d) => (d ? Math.round((n / d) * 1000) / 10 : 0);

// Dashboard column -> the Check tab's filter name for it. Check owns the row detail
// and the expanders, so a count links there rather than duplicating a table here.
const CHECK_PARAM = {
  outcome: "outcome", link_method: "link_method", type: "type",
  link_confidence: "link_confidence", doi_o_verification: "doi_verified",
};
const checkHref = (field, value) => {
  const name = CHECK_PARAM[field];
  return name ? `/check?${name}=${encodeURIComponent(value)}` : null;
};

// Categorical hues, ordered so the two verdicts a reader compares first —
// successful and failed — are furthest apart.
const SERIES = ["#2f6f9f", "#b3542e", "#7a8b3a", "#8a5a9e", "#3f8f7a",
                "#a8863c", "#6b7280", "#9e5570"];

async function getJSON(url) {
  const res = await fetch(url);
  if (!res.ok) throw new Error(`${url} → ${res.status}`);
  return res.json();
}

function prov(p) {
  if (!p) return "";
  if (p.state === "absent" || p.reason) {
    return `<div class="prov absent">${esc(p.reason || "not available here")}</div>`;
  }
  const bits = [p.source, p.state];
  if (p.release_id) bits.push("release " + p.release_id.slice(0, 12));
  if (p.as_of) bits.push(p.as_of);
  if (p.machine) bits.push("from " + p.machine);
  return `<div class="prov">${esc(bits.join(" · "))}</div>`;
}

const stat = (k, v, sub) => `<div class="stat"><div class="k">${esc(k)}</div>
  <div class="v">${num(v)}</div>${sub ? `<div class="sub">${esc(sub)}</div>` : ""}</div>`;

const panel = (title, body, opts = {}) => `<div class="panel ${opts.width || ""}">
  <h4>${esc(title)}</h4>${opts.hint ? `<div class="hint">${esc(opts.hint)}</div>` : ""}
  ${body}${prov(opts.prov)}</div>`;

// A distribution: proportion as the row's own background fill, value right-aligned.
function dist(counts, field) {
  const entries = Object.entries(counts || {}).filter(([, v]) => v !== undefined)
    .sort((a, b) => b[1] - a[1]);
  const total = entries.reduce((s, [, v]) => s + v, 0);
  if (!entries.length) return `<p class="loading">nothing to show</p>`;
  return `<div class="dist">` + entries.map(([k, v]) => {
    const inner = `<span>${esc(k)}</span><b>${num(v)}</b>
      <em>${total ? pct(v, total) + "%" : ""}</em>`;
    const style = `style="--p:${pct(v, total)}%"`;
    const href = field && checkHref(field, k);
    return href
      ? `<a class="r drill" ${style} href="${href}"
           title="Open these rows">${inner}</a>`
      : `<div class="r" ${style}>${inner}</div>`;
  }).join("") + `</div>`;
}

// ── donut ───────────────────────────────────────────────────────────────────
function donut(counts, centreLabel, field) {
  const entries = Object.entries(counts || {}).sort((a, b) => b[1] - a[1]);
  const total = entries.reduce((s, [, v]) => s + v, 0);
  if (!total) return `<p class="loading">nothing to show</p>`;
  const R = 50, C = 2 * Math.PI * R;
  let offset = 0;
  const arcs = entries.map(([, v], i) => {
    const len = (v / total) * C;
    const seg = `<circle r="${R}" cx="66" cy="66" fill="none"
      stroke="${SERIES[i % SERIES.length]}" stroke-width="20"
      stroke-dasharray="${len} ${C - len}" stroke-dashoffset="${-offset}"></circle>`;
    offset += len;
    return seg;
  }).join("");
  const legend = entries.map(([k, v], i) => {
    const inner = `<i style="background:${SERIES[i % SERIES.length]}"></i>
      <span>${esc(k)}</span><b>${num(v)}</b><em>${pct(v, total)}%</em>`;
    const href = field && checkHref(field, k);
    return href
      ? `<a class="r drill" href="${href}" title="Open these rows">${inner}</a>`
      : `<div class="r">${inner}</div>`;
  }).join("");
  return `<div class="chartwrap">
    <svg viewBox="0 0 132 132" class="donut" role="img"
         aria-label="${esc(entries.map(([k, v]) => `${k} ${v}`).join(", "))}">
      <g transform="rotate(-90 66 66)">${arcs}</g>
      <text x="66" y="63" class="donut-n">${num(total)}</text>
      <text x="66" y="77" class="donut-l">${esc(centreLabel)}</text>
    </svg>
    <div class="legend dist">${legend}</div></div>`;
}

// ── matrix ──────────────────────────────────────────────────────────────────
// Shared by the reproduction axes grid and the validation confusion matrices.
function matrix(rowLabels, colLabels, rows, opts = {}) {
  if (!rowLabels.length || !colLabels.length) return `<p class="loading">no data</p>`;
  const max = Math.max(1, ...rows.flat());
  // `cannot_be_determined` has no natural break, so it overflowed its column and
  // collided with the next header. <wbr> allows a break at the joints — and only
  // there, so nothing splits mid-word the way overflow-wrap did.
  const wrap = (t) => esc(t).replace(/_/g, "_<wbr>").replace(/ /g, " <wbr>");
  const head = colLabels.map((c) => `<th>${wrap(c)}</th>`).join("");
  const body = rowLabels.map((r, i) => {
    const cells = colLabels.map((c, j) => {
      const v = rows[i][j] || 0;
      if (!v) return `<td class="z">·</td>`;
      if (opts.diagonal) return `<td class="${r === c ? "ok" : "bad"}">${num(v)}</td>`;
      return `<td class="heat" style="--w:${v / max}">${num(v)}</td>`;
    }).join("");
    const sum = rows[i].reduce((a, b) => a + b, 0);
    return `<tr><th>${wrap(r)}</th>${cells}<td class="sum">${num(sum)}</td></tr>`;
  }).join("");
  return `<div class="mscroll"><table class="mx">
      <thead><tr><th>${esc(opts.corner || "")}</th>${head}<th class="sum">total</th></tr></thead>
      <tbody>${body}</tbody></table></div>
    ${opts.key ? `<p class="mxkey">${esc(opts.key)}</p>` : ""}`;
}

// ── flow ────────────────────────────────────────────────────────────────────
async function loadFlow() {
  const f = await getJSON("/api/dashboard/flow");
  $("release-badge").textContent =
    f.release_id ? "release " + f.release_id.slice(0, 12) : "no local release";

  $("funnel").innerHTML = f.stages.map((s) => {
    const absent = s.count === null || s.count === undefined;
    const reason = s.provenance && s.provenance.reason;
    return `<div class="step">
      <div class="k">${esc(s.label)}</div>
      <div class="v${absent ? " absent" : ""}">${absent ? "not on this machine" : num(s.count)}</div>
      ${reason ? `<div class="sub warn">${esc(reason)}</div>`
               : `<div class="sub">${esc((s.provenance && s.provenance.source) || "")}</div>`}
    </div>`;
  }).join("");

  const c = f.completeness || {};
  const gapRows = [];
  if ("no_text" in c) {
    // Not a Check link: a no_text work has no row in any CSV — it never reached
    // Stage 3. Stage 2 lists the routing rows themselves, which is all there is.
    gapRows.push(["matched a screen rule, no abstract (no_text)", c.no_text,
                  "/stage2#pending"]);
  }
  if ("blank_abstract_r" in c) {
    gapRows.push(["rendered rows with no abstract", c.blank_abstract_r,
                  "/check?no_abstract=1"]);
  }
  if ("blank_doi_r" in c) {
    gapRows.push(["rendered rows with no DOI", c.blank_doi_r, "/check?no_doi=1"]);
  }
  const gapsHtml = `<div class="dist">` + gapRows.map(([k, v, href]) => {
    const inner = `<span>${esc(k)}</span><b>${num(v)}</b><em></em>`;
    return href ? `<a class="r drill" href="${href}"
        title="Open these rows">${inner}</a>`
      : `<div class="r">${inner}</div>`;
  }).join("") + `</div>`;

  let html = panel("What the input was missing", gapsHtml, {
    width: c.by_pile ? "" : "full",
    hint: "no_text is recoverable coverage — those works would have been screened had "
        + "text existed. A blank abstract is the same gap further downstream: the "
        + "screen and every abstract-stage step read that field.",
  });
  if (c.by_pile) html += panel("Routed piles", dist(c.by_pile));
  $("completeness").innerHTML = html;

  const sets = Object.entries(f.set_aside || {})
    .sort((a, b) => (b[1].rows || 0) - (a[1].rows || 0));
  $("set-aside").innerHTML = sets.map(([file, s]) => `
    <details class="setcard${s.rows ? "" : " empty"}">
      <summary>
        ${s.rows
          ? `<a class="setname" href="/check?stage=${encodeURIComponent(
               file.replace(/\.csv$/, ""))}"
               title="Open these rows">${esc(s.title)}</a>`
          : `<span class="setname">${esc(s.title)}</span>`}
        <b>${num(s.rows)}</b>
        <span class="setfile">${esc(file)}</span>
      </summary>
      <div class="setbody">
        <p>${esc(s.why || "No description recorded for this destination.")}</p>
        ${s.action ? `<p><strong>What to do:</strong> ${esc(s.action)}</p>` : ""}
        <p><strong>Written when a row ends as:</strong>
          ${s.statuses.map((x) => `<code>${esc(x)}</code>`).join(" ")}</p>
      </div>
    </details>`).join("");
}

// ── analysis ────────────────────────────────────────────────────────────────
async function loadAnalysis() {
  const a = await getJSON("/api/dashboard/analysis");
  if (!a.rows) {
    $("analysis-stats").innerHTML =
      `<div class="stat"><div class="k">rendered rows</div>
       <div class="v">—</div></div>`;
    $("analysis").innerHTML = panel("Analysis", "", { width: "full", prov: a.provenance });
    return;
  }
  const rep = Object.values(a.by_outcome_replication).reduce((s, v) => s + v, 0);
  const ax = a.repro_axes || { computation: [], robustness: [], rows: [], total: 0 };

  $("analysis-stats").innerHTML =
    stat("rendered rows", a.rows) +
    stat("replications", rep, "coded on one outcome axis") +
    stat("reproductions", ax.total, "coded on two axes: computation and robustness") +
    `<div class="stat"><div class="k">read from</div>
      <div class="v" style="font-size:14px">extracted.csv</div>
      <div class="sub">live on every load · ${esc(
        (a.provenance && a.provenance.as_of) || "")}</div></div>`;

  $("analysis").innerHTML =
    panel("Outcome — replications", donut(a.by_outcome_replication, "replications",
      "outcome"), { width: "fifths",
                    hint: "The whole replication vocabulary. Click a slice or a row "
                        + "to open those papers in Check." }) +
    panel("Outcome — reproductions, by axis",
      matrix(ax.computation, ax.robustness, ax.rows, {
        corner: "computation ╲ robustness",
        key: "Shade shows relative size. A reproduction's outcome is the two axes "
           + "joined, so the flat list hides which axis failed — this does not.",
      }),
      { width: "sevenths", hint: `${ax.total} reproduction rows.` }) +
    panel("Link method", dist(a.by_link_method, "link_method"), { width: "third" }) +
    panel("Link confidence", dist(a.by_link_confidence, "link_confidence"),
      { width: "third" }) +
    panel("doi_o verification", dist(a.by_doi_verification, "doi_o_verification"),
      { width: "third" });
}

async function loadTokens() {
  const t = await getJSON("/api/dashboard/token-usage");
  if (!t.rows.length) {
    $("tokens").innerHTML = panel("Token usage", `<p class="loading">Nothing recorded
      on this machine. This ledger is per-checkout — it is not shared through the
      cache, so it fills only when a run spends here.</p>`,
      { width: "full", prov: t.provenance });
    return;
  }
  const byModel = Object.fromEntries(
    t.rows.map((r) => [`${r.provider} / ${r.model}`, r.total]));
  $("tokens").innerHTML =
    panel("Token usage by model", dist(byModel), {
      width: "full",
      hint: `${num(t.total)} tokens total — ${num(t.in)} in, ${num(t.out)} out.`
          + (t.openai_daily_budget
              ? ` OpenAI daily cap ${num(t.openai_daily_budget)}.` : ""),
      prov: t.provenance,
    });
}

// ── validation ──────────────────────────────────────────────────────────────
async function loadValidation() {
  const el = $("validation");
  try {
    const [s, conf, an] = await Promise.all([
      getJSON("/api/dashboard/supabase-stats"),
      getJSON("/api/dashboard/supabase-confusion"),
      getJSON("/api/dashboard/supabase-analytics"),
    ]);
    if (s.error) throw new Error(s.error);
    const votes = s.records_by_votes || {};

    $("validation-stats").innerHTML =
      stat("records", s.total) +
      stat("all 3 votes in", s.records_fully_voted,
        "Two humans and the LLM. Each record has three slots, so filled slots "
        + `(${num(s.total_judgements)}) count far more than records.`) +
      stat("both humans done", s.records_both_humans) +
      stat("finalised", s.validated);

    let matrices = "";
    let html = panel("Records by votes cast", dist({
      "3 of 3 — complete": votes["3"] || 0,
      "2 of 3": votes["2"] || 0,
      "1 of 3": votes["1"] || 0,
      "0 — untouched": votes["0"] || 0,
    }), { width: "full" });

    for (const [field, m] of Object.entries(conf || {})) {
      if (!m || !m.labels || !m.matrix) continue;
      matrices += panel(`${field} — pipeline vs final`,
        matrix(m.labels, m.labels, m.matrix, {
          diagonal: true, corner: "pipeline ╲ final",
          key: "Green diagonal = the pipeline matched. Every red cell is a "
             + "correction — read across a row to see what that category became.",
        }),
        // outcome has seven columns to type's three, so equal halves left the
        // wider one scrolling; the split follows the content.
        { width: field === "outcome" ? "sevenths" : "fifths",
          hint: `${m.accuracy}% match — ${num(m.correct)} of ${num(m.n)} records `
              + `finalised for ${field}. "Finalised" is per field, and is not the `
              + `same population as the validated count above.` });
    }

    el.innerHTML = html + matrices;
  } catch (err) {
    $("validation-stats").innerHTML = "";
    el.innerHTML = panel("Validation", `<p class="loading">Unreachable.</p>`,
      { width: "full", prov: { state: "absent", reason: err.message } });
  }
}

async function loadConcerns() {
  const c = await getJSON("/api/dashboard/concerns");
  const raised = c.concerns.filter((x) => x.count > 0).sort((a, b) => b.count - a.count);
  const clear = c.concerns.length - raised.length;
  // The COUNT is what a reader aims at — it is the biggest thing in the row and it is
  // the thing they want to see the rows behind. Linking only the label left the number
  // dead, so the whole count+label pair is the target where there is somewhere to go.
  $("concerns").innerHTML = raised.map((x) => `<li class="concern ${esc(x.severity)}">
      ${x.check_url
        ? `<a class="concern-link" href="${esc(x.check_url)}"
             title="Open these rows in Check"
             ><span class="count">${num(x.count)}</span
             ><span class="label">${esc(x.label)}</span></a>`
        : `<span class="count">${num(x.count)}</span>
           <span class="label">${esc(x.label)}</span>`}
      ${x.note ? `<div class="note">${esc(x.note)}</div>` : ""}
      ${x.command ? `<code>${esc(x.command)}</code>` : ""}
    </li>`).join("");
  $("concerns-clear").textContent = raised.length
    ? `${clear} further rules are clear.` : `All ${clear} rules are clear.`;
}

async function load() {
  await Promise.allSettled([loadFlow(), loadAnalysis(), loadTokens(),
                            loadValidation(), loadConcerns()]);
}
$("reload").addEventListener("click", load);

load();
