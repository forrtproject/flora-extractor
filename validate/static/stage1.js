// stage1.js — the Stage 1 panels. Shared helpers, the pipeline map, the issues
// panel and contents highlighting live in stage_common.js, which loads first.
//
// Nothing here states pipeline behaviour. The vocabulary, the schema and the release
// inputs are what /api/stage1 read out of the running code; the counts are what it
// read off this machine's artifacts, each carrying the provenance that says so.

// ── the gate ────────────────────────────────────────────────────────────────
// A stem outside Latin/Latin-Extended reaches works the English ones cannot; it is
// marked because that is the arm's whole justification, not decoration.
const isNonLatin = (s) => /[^ -ɏ]/.test(s);

function renderGate(d) {
  const el = $("gate-body");
  try {
    const g = d.gate || {};
    const hits = (d.breakdown && d.breakdown.gate_hits) || null;
    const total = (d.pool && d.pool.totals && d.pool.totals.total) || 0;

    el.className = "";
    el.innerHTML = `
      <div class="arm">
        <div class="arm-head"><h4>Arm 1 — the replication stem</h4>
          ${hits ? `<span class="hits">title ${n(hits.title)} · abstract ${
            n(hits.abstract)}</span>` : ""}</div>
        <p>One case-insensitive alternation of <strong>${g.stems.length} stems</strong>,
        matched against the title and against the <em>raw</em> abstract inverted-index
        JSON. Stems, never phrases: an inverted index is a <code>{word: [positions]}</code>
        dictionary whose key order is arbitrary, so words that are adjacent in the paper
        are not adjacent in the JSON and a phrase regex would match or miss by accident.</p>
        <ul class="stems">${g.stems.map((s) =>
          `<li class="${isNonLatin(s) ? "nonlatin" : ""}">${esc(s)}</li>`).join("")}</ul>
        <p class="mapscale">Outlined stems are non-Latin: 111 of 112 Korean 재검증 works
        carry no matching English stem at all, so they are invisible without them.</p>
      </div>

      <div class="arm">
        <div class="arm-head"><h4>Arm 2 — OpenAlex's own concepts</h4>
          ${hits && hits.concept != null ? `<span class="hits">${n(hits.concept)} rows · ${
            pct(hits.concept, total)} of the pool</span>` : ""}</div>
        <p>Works OpenAlex's classifier has already tagged as being about replication or
        reproducibility. This arm reaches papers whose text carries no matching stem —
        an absent abstract, or atypical wording.</p>
        <div class="scroll"><table class="doc">
          <thead><tr><th>Concept</th><th>What it is</th></tr></thead>
          <tbody>${g.concepts.map((c) => `
            <tr><td class="mono"><a href="${esc(c.url)}" target="_blank"
                rel="noopener">${esc(c.id)}</a></td>
                <td>${esc(c.note)}</td></tr>`).join("")}</tbody>
        </table></div>
      </div>

      <div class="path">
        <h4>The gate's fingerprint</h4>
        <span class="loc">${esc(g.fingerprint)}</span>
        <p>A hash of the stems and the concept ids together — the one part of Stage 1 a
        rescan can change. A partition marked done under one gate has <em>not</em> been
        read under another, and the rows it rejected were never stored anywhere, so
        recovering them means reading 725 GB again. This is the mismatch the scanner
        refuses on.</p>
      </div>
      ${provLine(d.breakdown_provenance)}
      <div class="note">The arms overlap — a work can trip both, so the three counts
      sum to more than the pool. They are three questions asked of each row, not three
      buckets.</div>`;
  } catch (err) { fail(el, err); }
}

// ── the pool ────────────────────────────────────────────────────────────────
function renderPool(d) {
  const el = $("pool-body");
  const schemaEl = $("schema-body");
  try {
    const totals = (d.pool && d.pool.totals) || null;
    const b = d.breakdown || {};
    el.className = "";

    if (!totals) {
      el.innerHTML = provLine(d.pool.provenance);
    } else {
      el.innerHTML = `<div class="stats">
        <div class="stat"><span class="k">Rows</span><span class="v">${n(totals.total)}</span>
          <span class="s">survivors</span></div>
        <div class="stat"><span class="k">Partitions</span><span class="v">${n(totals.files)}</span>
          <span class="s">one per snapshot file</span></div>
        <div class="stat"><span class="k">On disk</span><span class="v">${bytes(totals.bytes)}</span>
          <span class="s">zstd parquet</span></div>
        ${b.no_doi != null ? `<div class="stat"><span class="k">No DOI</span>
          <span class="v">${n(b.no_doi)}</span>
          <span class="s">${pct(b.no_doi, totals.total)} — keyed on the OpenAlex id instead</span></div>` : ""}
        ${totals.unreadable ? `<div class="stat"><span class="k">Unreadable</span>
          <span class="v">${n(totals.unreadable)}</span><span class="s">partitions</span></div>` : ""}
      </div>
      ${provLine(d.pool.provenance)}
      ${renderYears(b.by_year)}`;
    }

    schemaEl.className = "";
    schemaEl.innerHTML = `<div class="scroll"><table class="doc">
      <thead><tr><th>Column</th><th>Type</th><th>Role</th></tr></thead>
      <tbody>${d.schema.map((c) => `
        <tr><td class="mono">${esc(c.name)}</td>
            <td class="mono">${esc(c.type)}</td>
            <td>${esc(c.role)}</td></tr>`).join("")}</tbody>
    </table></div>
    <div class="note">The nested OpenAlex fields — <code>authorships</code>,
    <code>primary_location</code>, <code>open_access</code>, <code>concepts</code> —
    are stored as JSON strings, and <code>abstract_text</code> is the inverted index
    already reconstructed into reading order, so no later stage repeats that work.</div>`;
  } catch (err) {
    fail(el, err);
    if (schemaEl) fail(schemaEl, err);
  }
}

// The year spread, with the tails named rather than plotted: the pool carries works
// dated as far ahead as 2050, and a chart stretched to fit them shows nothing.
const YEAR_FROM = 1950;
function renderYears(byYear) {
  if (!byYear) return "";
  const years = Object.entries(byYear)
    .map(([y, c]) => [parseInt(y, 10), c]).filter(([y]) => !Number.isNaN(y));
  if (!years.length) return "";
  const now = new Date().getFullYear();
  const shown = years.filter(([y]) => y >= YEAR_FROM && y <= now).sort((a, b) => a[0] - b[0]);
  if (!shown.length) return "";
  const before = years.filter(([y]) => y < YEAR_FROM).reduce((s, [, c]) => s + c, 0);
  const after = years.filter(([y]) => y > now).reduce((s, [, c]) => s + c, 0);
  const max = Math.max(...shown.map(([, c]) => c), 1);
  return `<h3>When the survivors were published</h3>
    <div class="years">${shown.map(([y, c]) =>
      `<span class="y" style="height:${Math.max(1, 100 * c / max).toFixed(1)}%"
        title="${y}: ${n(c)}"></span>`).join("")}</div>
    <div class="yearaxis"><span>${shown[0][0]}</span><span>${now}</span></div>
    <div class="note">${n(before)} rows are dated before ${YEAR_FROM} and
    <b>${n(after)}</b> after ${now} — OpenAlex carries forward-dated records, and the
    gate applies no year cut, deliberately: a year filter here would be an exclusion,
    and exclusions are Stage 2's.</div>`;
}

// ── storage ─────────────────────────────────────────────────────────────────
function renderStorage(d) {
  const el = $("storage-body");
  try {
    const totals = (d.pool && d.pool.totals) || null;
    const side = (d.pool && d.pool.sidecar) || {};
    const ov = d.overlay || {};
    const repo = (d.remote && d.remote.repo) || null;

    const gateState = d.pool && d.pool.gate_matches_checkout;
    const gateNote = gateState === true
      ? `<div class="note ok">The pool's recorded gate matches this checkout's — the
         rows on disk are the rows this code would have admitted.</div>`
      : gateState === false
      ? `<div class="note bad">This pool was admitted under a <b>different</b> gate
         than this checkout computes. Legitimate — sharing a pool is the point — but
         the recorded gate is what names the release, not yours.</div>`
      : `<div class="note">The sidecar records no gate, so a release id built on this
         pool says <code>UNKNOWN</code> rather than assuming this checkout's. Stamp it
         with <code>python -m search.snapshot_scan --stamp-pool</code>.</div>`;

    el.className = "";
    el.innerHTML = `<div class="paths">
      <div class="path">
        <h4>The pool — parquet, flat, one file per snapshot partition</h4>
        <span class="loc">${esc(totals ? totals.pool_dir : "not on this machine")}</span>
        <p>Overridable with <code>FLORA_POOL_DIR</code>; it sits under the cache
        directory by default and is deliberately not created on import, because it is
        often pointed at an external disk.</p>
      </div>

      <div class="path">
        <h4>The sidecar — <code>_pool_provenance.json</code></h4>
        <p>What the dataset <em>is</em>, beside the dataset. Named with a leading
        underscore so the <code>*.parquet</code> globs every reader uses cannot feed it
        to pyarrow as pool data.</p>
        <dl>
          <dt>gate</dt><dd>${esc(side.search_gate_fingerprint || "—")}</dd>
          <dt>complete at</dt><dd>${side.expected_files != null
            ? `${n(side.expected_files)} files${totals
              ? ` · ${n(totals.files)} present` : ""}` : "—"}</dd>
          <dt>source</dt><dd>${esc(side.source || "—")}</dd>
          <dt>recorded</dt><dd>${esc(side.recorded_at || "—")}</dd>
        </dl>
      </div>

      <div class="path">
        <h4>The remote — a private Hugging Face dataset repo</h4>
        <span class="loc">${esc(repo || "FLORA_POOL_REPO is unset")}</span>
        <p>Sharded by year on the remote using the partition date in each file name, so
        <code>--years</code> is a genuinely partial download. Both directions are
        resumable: a file already present with the same size is skipped, so an
        interrupted transfer is restarted by re-running the same command.</p>
      </div>

      <div class="path">
        <h4>The text overlay — travels with the pool, is not a cache</h4>
        <span class="loc">${esc(ov.dir || "")}</span>
        <p>Text recovered from Crossref, Europe PMC, OSF and OpenAlex — for rows the
        snapshot shipped without an abstract, and for every admitted OSF record
        whatever text it already had. It is a <em>frozen release</em>, not a mutable
        table, because its hash is one of the six inputs to a routing release id.</p>
        <dl>
          <dt>chunks</dt><dd>${n(ov.chunks)}</dd>
          <dt>rows</dt><dd>${ov.rows != null ? n(ov.rows) : "—"}</dd>
          <dt>sources</dt><dd>${Object.entries(ov.sources || {})
            .map(([k, v]) => `${esc(k)} ${n(v)}`).join(" · ") || "—"}</dd>
          <dt>hash</dt><dd>${esc(ov.hash || (ov.reason ? "not frozen" : "—"))}</dd>
        </dl>
        ${ov.reason ? `<div class="note bad">${esc(ov.reason)}</div>` : ""}
      </div>
    </div>
    ${gateNote}`;
  } catch (err) { fail(el, err); }
}

// ── release inputs ──────────────────────────────────────────────────────────
const INPUT_NOTE = {
  pool_manifest_hash: "Stage 1's — the recorded gate plus every pool parquet's name, size and row count.",
  overlay_hash: "Stage 1's — the frozen text overlay, or null when there is none.",
  bundle_hash: "The JSON rule book, hashed whole.",
  engine_version: "The routing engine itself.",
  alias_release: "The OpenAlex work-id alias map that canonicalises merged ids.",
  schema_version: "The CSV column contract between stages.",
};

function renderRelease(d) {
  const el = $("release-body");
  try {
    el.className = "";
    el.innerHTML = `<div class="scroll"><table class="doc">
      <thead><tr><th>Release input</th><th>What it names</th><th>Whose</th></tr></thead>
      <tbody>${d.release_inputs.map((k) => {
        const mine = k === "pool_manifest_hash" || k === "overlay_hash";
        return `<tr><td class="mono">${esc(k)}</td>
          <td>${esc(INPUT_NOTE[k] || "")}</td>
          <td>${mine ? '<span class="pill admit">Stage 1</span>'
                     : '<span class="pill other">Stage 2</span>'}</td></tr>`;
      }).join("")}</tbody>
    </table></div>
    <div class="note">Anything that could move a row between piles is in that hash, so
    two runs with the same id are the same routing — and a changed pool or a new
    overlay mints a new id instead of silently overwriting the old decisions.</div>`;
  } catch (err) { fail(el, err); }
}

// ── commands ────────────────────────────────────────────────────────────────
const COMMANDS = [
  [".venv/bin/python -m search.pool_sync --pull",
   "Get the pool and its overlay. This is the one you run."],
  [".venv/bin/python -m search.pool_sync --push",
   "Publish a pool or a finished backfill. Refuses an unfrozen or stale overlay."],
  [".venv/bin/python -m search.run_search --scan",
   "The full 725 GB scan — 13-21 hours, resumable per partition through the ledger. " +
   "--scan is required so a bare invocation can never start one."],
  [".venv/bin/python -m search.snapshot_scan --status",
   "Progress of a scan in flight. Read-only and safe to run concurrently."],
  [".venv/bin/python -m search.snapshot_scan --stamp-pool",
   "Write the provenance sidecar for a pool that has none."],
  ['.venv/bin/python -c "from shared.dashboard_cache import refresh, POOL_STAGE; refresh(POOL_STAGE)"',
   "Recompute the per-arm and per-year breakdowns on this page."],
];

function renderCommands() {
  $("commands-body").innerHTML = `<div class="cmdlist">${COMMANDS.map(([c, why]) =>
    `<div class="cmdrow"><code>${esc(c)}</code><p>${esc(why)}</p></div>`).join("")}</div>`;
}

renderCommands();
trackSections();
renderIssues("stage-1");
getJSON("/api/stage1").then((d) => {
  renderPipelineMap("map-body", {
    snapshot: d.snapshot, poolTotals: d.pool && d.pool.totals,
    hereIds: ["pool"], hereLabel: "Stage 1",
  });
  renderGate(d); renderPool(d); renderStorage(d); renderRelease(d);
}).catch((err) => {
  ["map-body", "gate-body", "pool-body", "schema-body", "storage-body", "release-body"]
    .forEach((id) => { if ($(id)) fail($(id), err); });
});
