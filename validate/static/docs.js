// docs.js — fills the documentation page from the code-reading APIs.
//
// Nothing here describes pipeline behaviour. Every section renders what
// /api/docs/* read out of the running code, so the page cannot drift from it.
const $ = (id) => document.getElementById(id);
const esc = (s) => String(s ?? "").replace(/[&<>"]/g,
  (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
const n = (v) => Number(v ?? 0).toLocaleString();

// Piles read as three intentions. The colour says which, so a reader does not have
// to remember that "screen_expensive" is the one that can still admit.
const PILE_CLASS = {
  screen_expensive: "admit", screen_cheap: "admit", needs_human: "admit",
  discard: "drop", pending: "hold",
};
const pill = (name) =>
  `<span class="pill ${PILE_CLASS[name] || "other"}">${esc(name)}</span>`;

async function getJSON(url) {
  const res = await fetch(url);
  if (!res.ok) throw new Error(`${url} → ${res.status}`);
  return res.json();
}

function fail(el, err) {
  el.className = "failed";
  el.textContent = `Could not read this from the code: ${err.message}`;
}

// ── stage flow ──────────────────────────────────────────────────────────────
const STAGES = [
  ["search/", "Discovery", "Scans the OpenAlex snapshot into the survivor pool.",
   "python -m search.pool_sync --pull"],
  ["filter/engine/", "Routing and screening",
   "Rules sort the pool into piles; only the LLM screen may admit.",
   "python -m filter.engine route"],
  ["extract/", "Linking and coding",
   "A ladder finds each paper's original; the same reading codes the outcome.",
   "python -m extract.tier --release <id> --run"],
  ["validate/", "Monitoring",
   "This dashboard. Reads the store, the CSV and Supabase; writes nothing.",
   "python -m validate.app"],
];

function renderStages() {
  $("stageflow").innerHTML = STAGES.map(([pkg, title, blurb, cmd], i) => `
    <div class="stagerow">
      <span class="n">${i + 1}</span>
      <div>
        <h4>${esc(title)} <code>${esc(pkg)}</code></h4>
        <p>${esc(blurb)}</p>
        <span class="cmd">${esc(cmd)}</span>
      </div>
    </div>`).join("");
}

// ── architecture ────────────────────────────────────────────────────────────
async function renderArchitecture() {
  const el = $("architecture-body");
  try {
    const d = await getJSON("/api/docs/architecture");
    el.className = "";
    el.innerHTML = d.packages.map((p) => `
      <div class="pkg">
        <div class="pkg-head">
          <h3>${esc(p.title)}</h3>
          <span class="pkg-name">${esc(p.package)}/</span>
        </div>
        <p>${esc(p.blurb)}</p>
        <div class="scroll"><table class="doc">
          <thead><tr><th>Module</th><th>What it does</th><th>Lines</th></tr></thead>
          <tbody>${p.modules.map((m) => `
            <tr><td class="mono">${esc(m.name)}</td>
                <td>${esc(m.summary)}</td>
                <td class="num">${n(m.lines)}</td></tr>`).join("")}
          </tbody>
        </table></div>
      </div>`).join("");
  } catch (err) { fail(el, err); }
}

// ── piles, pending reasons, rules ───────────────────────────────────────────
const PENDING_MEANING = {
  no_filter_matched: "No rule in the book matched this work at all. It is not a " +
    "rejection — nothing has judged it yet. This is the bulk of the pool, and " +
    "narrowing it is what adding new rules does.",
  no_text: "A rule that would have sent this work for screening matched, but the " +
    "work has no abstract to screen. Absence of evidence must not become evidence " +
    "of absence, so it is held rather than discarded — recoverable coverage, once " +
    "a text overlay supplies the missing abstract.",
};

async function renderRules() {
  const pilesEl = $("piles-body");
  const pendEl = $("pending-body");
  const rulesEl = $("rules-body");
  try {
    const d = await getJSON("/api/docs/rules");
    const piles = (d.conventions && d.conventions.piles) || {};

    pilesEl.className = "";
    pilesEl.innerHTML = `<div class="scroll"><table class="doc">
      <thead><tr><th>Pile</th><th>Paper type</th><th>Confidence</th>
      <th>Exported</th><th>Can still be admitted</th></tr></thead>
      <tbody>${Object.entries(piles).map(([name, v]) => `
        <tr><td>${pill(name)}</td>
            <td class="mono">${esc(v.paper_type || "—")}</td>
            <td class="mono">${esc(v.filter_confidence || "—")}</td>
            <td>${v.exported ? "yes" : "no"}</td>
            <td>${PILE_CLASS[name] === "admit" ? "yes — the screen votes on it"
                  : name === "pending" ? "not yet — nothing has judged it"
                  : "no"}</td></tr>`).join("")}
      </tbody></table></div>`;

    const reasons = (d.conventions && d.conventions.pending_reasons) || [];
    pendEl.className = "";
    pendEl.innerHTML = reasons.map((r) => `
      <p><code>${esc(r)}</code> — ${esc(PENDING_MEANING[r] || "")}</p>`).join("") ||
      "<p>No pending reasons declared.</p>";

    rulesEl.className = "";
    rulesEl.innerHTML = d.rules.map((r) => `
      <details class="rule">
        <summary>
          <span class="rid">${esc(r.id)}</span>
          ${r.pile ? pill(r.pile) : ""}
          <span class="prec">${r.shadow ? "shadow · " : ""}precedence ${
            r.precedence ?? "—"}</span>
        </summary>
        <div class="rule-detail">
          ${r.shadow ? `<p class="shadow-note">Shadow rule — it is evaluated and
            recorded, but does not move the work.</p>` : ""}
          <p>${esc(r.description || "No description recorded.")}</p>
          ${r.measured ? `<p class="frag-label">measured</p>
            <pre>${esc(JSON.stringify(r.measured, null, 2))}</pre>` : ""}
          <p class="frag-label">match — ${esc(r.file)}</p>
          <pre>${esc(JSON.stringify(r.match, null, 2))}</pre>
        </div>
      </details>`).join("");
  } catch (err) {
    [pilesEl, pendEl, rulesEl].forEach((el) => fail(el, err));
  }
}

// ── ladder ──────────────────────────────────────────────────────────────────
async function renderLadder() {
  const el = $("ladder-body");
  try {
    const d = await getJSON("/api/docs/ladder");
    el.className = "";
    el.innerHTML = `
      <p>Ladder version <strong>${n(d.version)}</strong>. A link counts as
      <em>resolved</em> — and so may be outcome-coded and exported — only when its
      method is one of these ${d.resolved_link_methods.length}:</p>
      <div class="scroll"><table class="doc">
        <thead><tr><th>Resolved link method</th></tr></thead>
        <tbody>${d.resolved_link_methods.map((m) =>
          `<tr><td class="mono">${esc(m)}</td></tr>`).join("")}</tbody>
      </table></div>
      <h3>Why each step behaves the way it does</h3>
      <p>The running record kept beside the ladder version, newest first.</p>
      <ol class="ladder">${d.revisions.map((r) => `
        <li><span class="n">${r.n}</span><p>${esc(r.text)}</p></li>`).join("")}
      </ol>`;
  } catch (err) { fail(el, err); }
}

// ── prompts ─────────────────────────────────────────────────────────────────
async function renderPrompts() {
  const el = $("prompts-body");
  try {
    const d = await getJSON("/api/docs/prompts");
    el.className = "";
    el.innerHTML = d.prompts.map((p) => `
      <details class="prompt">
        <summary>
          <span class="pname">${esc(p.name)}</span>
          <span class="pmeta">${esc(p.kind)}${
            p.fragments.length ? ` · ${p.fragments.length} fragment${
              p.fragments.length === 1 ? "" : "s"}` : ""}</span>
          <span class="pmeta">v${esc(p.version)} · ${n(p.lines)} lines</span>
        </summary>
        <div class="prompt-detail">
          <p class="frag-label">${p.kind === "builder"
            ? "assembly" : "prompt text"}</p>
          <pre>${esc(p.body)}</pre>
          ${p.fragments.map((f) => `
            <p class="frag-label">${esc(f.name)} — ${n(f.lines)} lines</p>
            <pre>${esc(f.text)}</pre>`).join("")}
        </div>
      </details>`).join("");
  } catch (err) { fail(el, err); }
}

// ── contents highlighting ───────────────────────────────────────────────────
function trackSections() {
  const links = [...document.querySelectorAll(".toc a")];
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

renderStages();
trackSections();
Promise.allSettled([renderArchitecture(), renderRules(), renderLadder(),
                    renderPrompts()]);
