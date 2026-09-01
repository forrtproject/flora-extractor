// stage3.js — the Stage 3 panels. Shared helpers, the pipeline map, the issues panel
// and contents highlighting live in stage_common.js, which loads first.
//
// The ladder, the vocabularies and the models are what /api/stage3 read out of the
// running code; every count is read off data/extracted.csv on this machine, live on
// each request, and carries the provenance that says so.

// A distribution as a ranked bar list. Used for four different breakdowns, which is
// why it is a function and not four blocks of markup.
function bars(counts, { limit = 40, total = null, muted = null } = {}) {
  const rows = Object.entries(counts || {}).sort((a, b) => b[1] - a[1]).slice(0, limit);
  if (!rows.length) return "";
  const max = Math.max(...rows.map(([, c]) => c), 1);
  const sum = total || rows.reduce((a, [, c]) => a + c, 0);
  return `<div class="dist">${rows.map(([k, c]) => `
    <div class="distrow${muted && muted(k) ? " muted" : ""}">
      <span class="dk mono">${esc(k)}</span>
      <span class="dbar"><span style="width:${(100 * c / max).toFixed(1)}%"></span></span>
      <span class="dv">${n(c)}</span>
      <span class="dp">${pct(c, sum)}</span>
    </div>`).join("")}</div>`;
}

// ── the two questions: which models answer them ─────────────────────────────
function renderModels(d) {
  const el = $("models-body");
  try {
    const m = d.models || {};
    el.className = "";
    el.innerHTML = `<h3>One model per call site, and no fallback</h3>
      <div class="scroll"><table class="doc">
        <thead><tr><th>Call site</th><th>The question it answers</th><th>Model</th>
          <th>Effort</th></tr></thead>
        <tbody>${(m.calls || []).map((c) => `
          <tr><td class="mono">${esc(c.site)}</td>
              <td>${esc(c.asks)}</td>
              <td class="mono">${esc(c.model)}</td>
              <td class="mono">${esc(c.effort || "—")}</td></tr>`).join("")}
        </tbody></table></div>
      <div class="note">Each constant is the <em>only</em> model that can answer its
      call. Retries go to the same model; when they are exhausted the row records the
      failure. A provider ladder used to run Gemini → OpenAI → OpenRouter, which made an
      outage invisible: the row got an answer from a model no evaluation covered, and
      the cache key had to over-name every model that might have produced it. The
      reasoning effort belongs to the call site, not the model — two sites may name the
      same model, and the two settings never share a cache entry.</div>
      <div class="stats">
        <div class="stat"><span class="k">Rows in flight</span>
          <span class="v">${m.workers ?? "—"}</span>
          <span class="s">EXTRACT_WORKERS; 1 disables the pool</span></div>
      </div>`;
  } catch (err) { fail(el, err); }
}

// ── the ladder ──────────────────────────────────────────────────────────────
function renderLadder(d) {
  const el = $("ladder-body");
  try {
    const counts = (d.counts && d.counts.link_method) || {};
    const rows = d.counts && d.counts.rows;
    el.className = "";
    el.innerHTML = `<ol class="steps">${(d.ladder || []).map((r, i) => {
      const got = counts[r.method] || 0;
      return `<li class="step${got ? "" : " cold"}">
        <span class="rn">${i + 1}</span>
        <div class="rbody">
          <div class="rhead">
            <span class="rname">${esc(r.name)}</span>
            <code>${esc(r.method)}</code>
            ${r.known ? "" : '<span class="tag warn">not in schema</span>'}
            <span class="rcount">${got ? `${n(got)} rows` : "none here"}</span>
          </div>
          <p>${esc(r.blurb)}</p>
          <span class="rcost">cost: ${esc(r.cost)}</span>
        </div></li>`;
    }).join("")}</ol>
      ${rows ? `<div class="note">Percentages of the ${n(rows)} shipped rows:
        ${Object.entries(counts).sort((a, b) => b[1] - a[1])
          .map(([k, c]) => `<code>${esc(k)}</code> ${pct(c, rows)}`).join(" · ")}.
        A step reading "none here" either never fires on this corpus or was superseded
        by a later ladder version.</div>` : ""}
      <div class="stats">
        <div class="stat"><span class="k">Ladder version</span>
          <span class="v">${esc(String(d.ladder_version ?? "—"))}</span>
          <span class="s">provenance only — a bump reopens nothing by itself</span></div>
        <div class="stat"><span class="k">Methods that count as resolved</span>
          <span class="v">${((d.methods && d.methods.resolved) || []).length}</span>
          <span class="s">only these may be outcome-coded and exported</span></div>
      </div>`;
  } catch (err) { fail(el, err); }
}

async function renderRevisions() {
  const el = $("revisions-body");
  try {
    const d = await getJSON("/api/docs/ladder");
    el.className = "";
    // Collapsed by default: this is the project's own history of the extraction
    // pipeline, valuable to read and long enough to bury everything under it.
    const revs = d.revisions || [];
    const newest = revs.slice(0, 2);
    const rest = revs.slice(2);
    const item = (r) => `<li><span class="n">${r.n}</span><p>${esc(r.text)}</p></li>`;
    el.innerHTML = `<ol class="ladder">${newest.map(item).join("")}</ol>
      ${rest.length ? `<details class="changelog">
        <summary><span class="cg-open">Show</span
          ><span class="cg-shut">Hide</span> the earlier ${rest.length} entries
          <span class="cg-hint">— ladder ${revs[revs.length - 1].n} to ${
            rest[0].n}</span></summary>
        <ol class="ladder">${rest.map(item).join("")}</ol>
      </details>` : ""}`;
  } catch (err) { fail(el, err); }
}

// ── outcome descent ─────────────────────────────────────────────────────────
function renderDescent(d) {
  const el = $("descent-body");
  try {
    el.className = "";
    el.innerHTML = `<div class="gaterules">
      <div class="gaterule keep"><span class="gk">ends the row</span>
        <p>The step resolved the link <em>and</em> the outcome is settled — for a
        replication, <code>outcome</code> is not <code>cannot_be_determined</code>; for
        a reproduction, neither axis is unsettled.</p></div>
      <div class="gaterule hold"><span class="gk">carries on</span>
        <p>The step accepted a link but could not settle the verdict. The resolution is
        <em>carried</em> and the ladder keeps descending towards the sections that state
        the outcome.</p></div>
    </div>
    <div class="note"><code>OUTCOME_DESCENT</code> is
    <b>${d.outcome_descent ? "on" : "off"}</b>, and it is not a tunable — it changes
    what a row is <em>coded as</em>, so it is a constant changed by a commit.</div>`;
  } catch (err) { fail(el, err); }
}

// ── documents ───────────────────────────────────────────────────────────────
const STRUCTURED = {
  openalex_xml: "OpenAlex GROBID XML — passes on any section text or any reference.",
  epmc_xml: "Europe PMC JATS full text — passes on any section text or any reference.",
  osf_registration: "The OSF registration form from the API — needs at least 1,000 characters of description plus form fields.",
  html_landing: "The row's own page, parsed with lxml — needs 10,000+ characters BEYOND the abstract, or a 2,000+ character reference block.",
};

function renderPdf(d) {
  const el = $("pdf-body");
  try {
    const src = (d.counts && d.counts.pdf_source) || {};
    const parse = (d.counts && d.counts.parse_method) || {};
    const rows = (d.counts && d.counts.rows) || 0;
    const none = src["(none)"] || 0;
    el.className = "";
    el.innerHTML = `<div class="stats">
        <div class="stat"><span class="k">Rows needing no document</span>
          <span class="v">${n(none)}</span>
          <span class="s">${pct(none, rows)} — resolved before the full-text step</span></div>
        <div class="stat"><span class="k">Rows with a document</span>
          <span class="v">${n(rows - none)}</span>
          <span class="s">across ${Math.max(Object.keys(src).length - 1, 0)} sources</span></div>
      </div>
      <h3>Which source supplied it</h3>
      ${bars(src, { total: rows, muted: (k) => k === "(none)" })}
      <h3>Which parser won</h3>
      <p>Every method that can read the document is run and the best result wins on
      score, so <code>parse_method</code> is an outcome, not a setting.</p>
      ${bars(parse, { total: rows, muted: (k) => k === "(none)" })}
      <h3>The four sources that return no file</h3>
      <div class="scroll"><table class="doc">
        <thead><tr><th>Source</th><th>What it is, and the check it must pass</th></tr></thead>
        <tbody>${Object.entries(STRUCTURED).map(([k, v]) => `
          <tr><td class="mono">${esc(k)}</td><td>${esc(v)}</td></tr>`).join("")}
        </tbody></table></div>
      <div class="note">A <code>llm_fulltext</code> row with a blank
      <code>pdf_source</code> would be a contradiction — both fields are full-text
      provenance, and they are blank together or not at all.</div>`;
  } catch (err) { fail(el, err); }
}

// ── vocabularies ────────────────────────────────────────────────────────────
function renderVocab(d) {
  const el = $("vocab-body");
  try {
    const v = d.vocabularies || {};
    const types = (d.counts && d.counts.type) || {};
    const chips = (list) => `<ul class="stems">${(list || []).map((x) =>
      `<li>${esc(x)}</li>`).join("")}</ul>`;
    el.className = "";
    el.innerHTML = `<div class="arm">
        <div class="arm-head"><h4>Replication — one axis</h4>
          <span class="hits">${n(types.replication || 0)} rows</span></div>
        <p>Did the re-test find what the original found?</p>
        ${chips(v.replication)}
      </div>
      <div class="arm">
        <div class="arm-head"><h4>Reproduction — two independent axes</h4>
          <span class="hits">${n(types.reproduction || 0)} rows</span></div>
        <p>A reproduction re-runs the original data and code, so two separate questions
        apply and each is coded in its own column with its own quote.</p>
        <p class="frag-label">computation — did re-running the analysis produce the
        original numbers?</p>
        ${chips(v.computation)}
        <p class="frag-label">robustness — does the finding survive alternative
        reasonable specifications?</p>
        ${chips(v.robustness)}
      </div>
      ${types["(none)"] ? `<div class="note">${n(types["(none)"])} rows carry no
        <code>type</code> at all: nothing — neither the screen nor Stage 2 — assigned
        one, and such rows are not ready for validation.</div>` : ""}`;
  } catch (err) { fail(el, err); }
}

// ── confidence and verification ─────────────────────────────────────────────
function renderConfidence(d) {
  const el = $("confidence-body");
  try {
    const c = d.counts || {};
    const rows = c.rows || 0;
    el.className = "";
    el.innerHTML = `<h3>What the checks produced</h3>
      <div class="twoup">
        <div><p class="frag-label">link_confidence</p>${bars(c.link_confidence, { total: rows })}</div>
        <div><p class="frag-label">doi_o verification</p>${bars(c.doi_o_verification, { total: rows })}</div>
      </div>
      ${provLine(d.provenance)}`;
  } catch (err) { fail(el, err); }
}

// ── set-asides ──────────────────────────────────────────────────────────────
async function renderSetAside(d) {
  const el = $("setaside-body");
  try {
    const flow = await getJSON("/api/dashboard/flow").catch(() => ({}));
    const counts = flow.set_aside || {};
    const files = (d.methods && d.methods.set_aside) || [];
    el.className = "";
    el.innerHTML = files
      .map((f) => ({ ...f, rows: (counts[f.file] || {}).rows ?? null }))
      .sort((a, b) => (b.rows || 0) - (a.rows || 0))
      .map((f) => `<div class="aside${f.rows ? "" : " empty"}">
        <div class="aside-head">
          <code>${esc(f.file)}</code>
          <span class="aside-count">${f.rows == null ? "—" : n(f.rows)}</span>
        </div>
        <p>${esc(f.meaning)}</p>
        <p class="aside-meta">${f.settles
          ? "Settles the work — a re-run buys the same answer."
          : "Does NOT settle the work."} Statuses: ${
            f.statuses.map((s) => `<code>${esc(s)}</code>`).join(" ")}</p>
      </div>`).join("") +
      `<div class="note">Several statuses can share one file — <code>non_article</code>
       and <code>non_article_type</code> both land in
       <code>not_a_replication.csv</code> — so the destinations are de-duplicated
       before counting. A file the export wrote nothing to is deleted, so absence
       means an empty pile: 0, not unavailable.</div>`;
  } catch (err) { fail(el, err); }
}

// ── the export ──────────────────────────────────────────────────────────────
function renderExport(d) {
  const el = $("export-body");
  try {
    const c = d.counts || {};
    el.className = "";
    el.innerHTML = `<div class="stats">
        <div class="stat"><span class="k">Rendered rows</span>
          <span class="v">${n(c.rows || 0)}</span>
          <span class="s">replication–original pairs</span></div>
        <div class="stat"><span class="k">Replications</span>
          <span class="v">${n((c.type || {}).replication || 0)}</span>
          <span class="s">one outcome axis</span></div>
        <div class="stat"><span class="k">Reproductions</span>
          <span class="v">${n((c.type || {}).reproduction || 0)}</span>
          <span class="s">two axes</span></div>
      </div>
      ${provLine(d.provenance)}`;
  } catch (err) { fail(el, err); }
}

// ── commands ────────────────────────────────────────────────────────────────
const COMMANDS = [
  [".venv/bin/python -m extract.tier",
   "DRY RUN. Prints the worklist size and what it would buy. Free — and the check " +
   "that settles what a run will actually extract."],
  [".venv/bin/python -m extract.tier --run --mode validation",
   "The sandbox. Records real verdicts the live export ignores. The first run of " +
   "changed Stage 3 code goes through here; re-running live is the promotion, and it " +
   "is near-free on cached calls."],
  [".venv/bin/python -m extract.tier --run",
   "The live campaign: claims works, runs the ladder, stores one permanent verdict each."],
  [".venv/bin/python -m extract.tier --run --redo-status unidentified_original",
   "Reopen a named population. --redo and --redo-status ADD to the worklist; only " +
   "--only restricts it."],
  [".venv/bin/python -m extract.export --release <id>",
   "Render the stored verdicts into data/extracted.csv — whole, sorted, atomic. The " +
   "only writer of that file."],
  [".venv/bin/python -m extract.export --release <id> --check",
   "Compare what the export would render against what is on disk, writing nothing."],
  [".venv/bin/python -m extract.sanity_check",
   "The integrity report over the exported CSV. It moves nothing."],
  [".venv/bin/python -m extract.audit_dois --apply",
   "The only thing that re-verifies a settled row; writes a correcting verdict that " +
   "supersedes the old one."],
];

function renderCommands() {
  $("commands-body").innerHTML = `<div class="cmdlist">${COMMANDS.map(([c, why]) =>
    `<div class="cmdrow"><code>${esc(c)}</code><p>${esc(why)}</p></div>`).join("")}</div>`;
}

renderCommands();
trackSections();
renderIssues("stage-3");
renderPrompts("prompts-body", [
  "build_target_outcome_prompt", "build_repro_target_outcome_prompt",
  "build_outcome_prompt", "build_repro_outcome_prompt",
  "build_author_year_pick_prompt", "build_keyed_confirm_prompt",
  "build_search_confirm_prompt",
  "PDF_REFERENCES_PROMPT", "PDF_IMAGE_REFERENCES_PROMPT",
], {
  build_target_outcome_prompt: "The ladder's main call, for REPLICATIONS: names the " +
    "targets and codes each one's outcome from the same reading. Serves the abstract, " +
    "reference-list and full-text steps alike.",
  build_repro_target_outcome_prompt: "The same call for REPRODUCTIONS, coding the two " +
    "independent axes instead of one.",
  build_outcome_prompt: "The standalone coder, for links a deterministic rule resolved " +
    "— where nothing has read the paper and checked the original. The original is given " +
    "as evidence to CHECK.",
  build_repro_outcome_prompt: "The standalone coder in the reproduction vocabulary.",
  build_author_year_pick_prompt: "Adjudicates the pooled author-and-year shortlist, " +
    "with decline offered first-class.",
  build_keyed_confirm_prompt: "The cold second opinion on an LLM-accepted keyed link: " +
    "shown only the study, the quoted evidence and the record.",
  build_search_confirm_prompt: "Grades a pooled-search link on four levels. The grade " +
    "sets link_confidence, so this prompt is in the generation fingerprint.",
  PDF_REFERENCES_PROMPT: "Pulls the reference list out of a parsed document.",
  PDF_IMAGE_REFERENCES_PROMPT: "The same, for a document that only parsed as images.",
});
renderRevisions();
getJSON("/api/stage3").then((d) => {
  renderPipelineMap("map-body", {
    snapshot: {}, poolTotals: null,
    hereIds: ["rendered"], hereLabel: "Stage 3",
  });
  renderModels(d); renderLadder(d); renderDescent(d); renderPdf(d);
  renderVocab(d); renderConfidence(d); renderSetAside(d); renderExport(d);
}).catch((err) => {
  ["map-body", "models-body", "ladder-body", "descent-body", "pdf-body", "vocab-body",
   "confidence-body", "setaside-body", "export-body"]
    .forEach((id) => { if ($(id)) fail($(id), err); });
});
