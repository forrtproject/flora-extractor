// stage2.js — the Stage 2 panels. Shared helpers, the pipeline map, the issues panel
// and contents highlighting live in stage_common.js, which loads first.
//
// Every rule, pile and voter is what /api/stage2 read out of the specs and constants
// the engine itself loads; every count is what it read off this machine's routing
// store and the screen's verdict rows, each carrying the provenance that says so.

// Piles read as three intentions. The colour says which, so a reader does not have to
// remember that `screen_expensive` is the one that can still admit. Same mapping the
// documentation page uses.
const PILE_CLASS = {
  screen_expensive: "admit", screen_cheap: "admit", needs_human: "admit",
  discard: "drop", pending: "hold",
};
const pill = (name) =>
  `<span class="pill ${PILE_CLASS[name] || "other"}">${esc(name)}</span>`;

// ── the funnel ──────────────────────────────────────────────────────────────
// The question this page exists to answer: what happens to five million rows. Each
// step is a survivor count plus the loss that produced it, so the two always add up
// on screen — a funnel whose steps do not reconcile is a funnel nobody can check.
function step({ label, count, note, cls, sub }) {
  return `<div class="fstep ${cls || ""}">
    <div class="fstep-head">
      <span class="fstep-label">${label}</span>
      <span class="fstep-count">${count == null ? "—" : n(count)}</span>
    </div>
    ${sub ? `<p class="fstep-sub">${sub}</p>` : ""}
    ${note ? `<p class="fstep-note">${note}</p>` : ""}
  </div>`;
}

const drop = (count, label, detail) => `<div class="fdrop">
  <span class="fdrop-count">−${n(count)}</span>
  <span class="fdrop-body"><b>${esc(label)}</b>${detail ? ` — ${detail}` : ""}</span>
</div>`;

// *verdicts* arrives on a second, slower request (Postgres, ~30 s cold), so the funnel
// is drawn twice: once ending at the admitted pile with the screen steps pending, and
// again with them filled. Waiting for it would hold the whole page behind the slowest
// read on the page.
function renderFunnel(d, verdicts, verdictProv) {
  const el = $("funnel-body");
  try {
    const rt = d.routing || {};
    const piles = rt.piles || {};
    const reasons = rt.pending_reasons || {};
    const v = verdicts || {};
    const poolTotal = d.pool && d.pool.total;
    const routed = Object.values(piles).reduce((a, b) => a + b, 0) || null;
    const admitted = piles.screen_expensive || 0;
    const pending = piles.pending || 0;

    if (!routed) {
      el.className = "";
      el.innerHTML = provLine(d.store_provenance);
      return;
    }

    const aliasNote = d.aliases != null && poolTotal
      ? `exactly the ${n(d.aliases)} entries in <code>filter/spec/aliases.json</code> — ` +
        "routing keys by the alias-resolved work id, so each alias folds two pool rows " +
        "into one work. Deduplication, not loss."
      : "merged OpenAlex work ids folding onto their canonical id.";

    el.className = "funnel";
    el.innerHTML = [
      step({
        label: "Survivor pool", count: poolTotal, cls: "start",
        sub: "what Stage 1 handed over",
      }),
      poolTotal && routed ? drop(poolTotal - routed, "alias merges", aliasNote) : "",
      step({
        label: "Routed works", count: routed,
        sub: "every one assigned to exactly one pile",
      }),
      drop(piles.discard || 0, "discarded by a rule",
        "a rule said this is not a paper, or not a study — kept in the routing table " +
        "with the rule and the matched text, never deleted"),
      drop(pending, "left pending",
        `nothing judged them: ${n(reasons.no_filter_matched || 0)} matched no rule at ` +
        `all, ${n(reasons.no_text || 0)} matched a screening rule but had no abstract ` +
        "to screen"),
      step({
        label: "Admitted for screening", count: admitted, cls: "mid",
        sub: "the only rows an LLM is allowed to cost money on",
        note: poolTotal
          ? `${pct(admitted, poolTotal)} of the pool. Everything above this line was ` +
            "decided by cheap rules over columns; everything below costs model calls."
          : "",
      }),
      v.settled != null
        ? drop(v.dropped || 0, "discarded by the screen",
            "both voters answered <code>none</code> — the gate is unanimous, so no " +
            "single voter discards alone")
        : "",
      v.settled != null
        ? step({
            label: "Passed the screen", count: v.proceeded, cls: "end",
            sub: "Stage 3's worklist — rebuilt in process, with the screen's answer on the row",
            note: Object.entries(v.by_record_type || {})
              .sort((a, b) => b[1] - a[1])
              .map(([k, c]) => `${esc(k)} ${n(c)}`).join(" · "),
          })
        : verdictProv
          ? `<div class="note">The screen's verdicts could not be read here, so this
             funnel stops at the admitted pile. ${esc(verdictProv.reason || "")}</div>`
          : `<div class="fpending">Reading the screen's verdicts from the state
             authority — this one is slow, the rest of the page is not.</div>`,
    ].join("") + (verdictProv ? provLine(verdictProv) : "");
  } catch (err) { fail(el, err); }
}

// ── piles ───────────────────────────────────────────────────────────────────
const PILE_MEANING = {
  discard: "A rule was confident this is not a candidate. Costs nothing, and cannot be admitted later without a rule change.",
  pending: "Nothing has judged it. Not a rejection — the bulk of the pool sits here, and narrowing it is what adding rules does.",
  screen_cheap: "Queued for the cheap discard-only tier. Dormant: all three specs that route here are shadow.",
  screen_expensive: "Queued for the two-voter screen. This is where the money is.",
  needs_human: "Queued for a person. The screen still votes on it.",
};

async function renderPiles(d) {
  const el = $("piles-body");
  try {
    const rules = await getJSON("/api/docs/rules");
    const conv = (rules.conventions && rules.conventions.piles) || {};
    const counts = (d.routing && d.routing.piles) || {};
    const total = Object.values(counts).reduce((a, b) => a + b, 0);
    const admitted = new Set(d.admitted_piles || []);

    el.className = "";
    el.innerHTML = `<div class="scroll"><table class="doc">
      <thead><tr><th>Pile</th><th>On this release</th><th>Paper type</th>
        <th>Costs money</th><th>What it means</th></tr></thead>
      <tbody>${Object.entries(conv).map(([name, meta]) => `
        <tr><td>${pill(name)}</td>
            <td class="num">${counts[name] != null
              ? `${n(counts[name])}<span class="sub"> · ${pct(counts[name], total)}</span>`
              : "0"}</td>
            <td class="mono">${esc(meta.paper_type || "—")}</td>
            <td>${admitted.has(name) ? "yes" : "no"}</td>
            <td>${esc(PILE_MEANING[name] || "")}</td></tr>`).join("")}
      </tbody></table></div>
      <div class="note">"Costs money" is <code>ADMITTED_PILES</code> in
      <code>filter/engine/route.py</code>: a screen call, or someone's reading time.
      <code>discard</code> and <code>pending</code> spend nothing, which is what makes
      an unintended <em>admission</em> the expensive direction of a routing mistake.</div>`;
  } catch (err) { fail(el, err); }
}

// ── the rule book ───────────────────────────────────────────────────────────
// Each rule is a <details>: the summary is the countable row, the body is what the
// spec actually SAYS — its description, the evidence it was measured against, and the
// match tree the engine evaluates. Same source as the documentation page.
//
// The four counts are an identity, and that is the point of showing them together:
//
//     matched = won + downgraded + outranked
//
// so a gap always has a named cause rather than looking like a rule that failed.
async function renderRulebook(d) {
  const el = $("rulebook-body");
  try {
    const data = await getJSON("/api/docs/rules");
    const rt = d.routing || {};
    // A rule can win rows into two piles — its own, and `pending` for the rows the
    // no-text downgrade took off it. Both are kept, because that pairing is the
    // clearest picture of what the downgrade actually costs a rule.
    const won = {};
    (rt.by_rule || []).forEach((r) => {
      (won[r.rule_id] = won[r.rule_id] || {})[r.pile] = r.count;
    });
    const matched = Object.fromEntries(
      (rt.evaluations || []).map((e) => [e.spec_id, e.matched]));

    el.className = "";
    el.innerHTML = `<div class="rulehead">
        <span>Rule</span><span>Pile</span><span class="num">Prec.</span>
        <span class="num">Matched</span><span class="num">Won</span>
        <span class="num">Downgr.</span><span class="num">Outranked</span>
      </div>` + [...data.rules].sort((a, b) => (b.precedence ?? 0) - (a.precedence ?? 0))
      .map((r) => {
      const w = won[r.id] || {};
      const main = w[r.pile] || 0;
      const held = w.pending || 0;
      const hit = matched[r.id];
      // Exact, not estimated: every work a live rule matched has exactly one winner,
      // and that winner outranks this rule whenever it is not this rule.
      const outranked = (!r.shadow && hit != null) ? hit - main - held : null;
      return `<details class="rule2${r.shadow ? " shadow-row" : ""}">
        <summary>
          <span class="rid">${esc(r.id)}${r.shadow
            ? ' <span class="tag">shadow</span>' : ""}</span>
          <span>${r.pile ? pill(r.pile) : "—"}</span>
          <span class="num">${r.precedence ?? "—"}</span>
          <span class="num">${hit != null ? n(hit) : "—"}</span>
          <span class="num">${r.shadow ? "—" : n(main)}</span>
          <span class="num">${held ? n(held) : "·"}</span>
          <span class="num">${outranked ? n(outranked) : (r.shadow ? "—" : "·")}</span>
        </summary>
        <div class="rule-detail">
          ${r.shadow ? `<p class="shadow-note">Shadow rule — evaluated and recorded,
            but it moves nothing. Its <b>Matched</b> is what it would have claimed if
            promoted.</p>` : ""}
          <p>${esc(r.description || "No description recorded in the spec.")}</p>
          ${r.vocabulary ? `<p class="frag-label">vocabulary</p>
            <pre>${esc(r.vocabulary)}</pre>` : ""}
          ${r.measured ? `<p class="frag-label">measured — what this rule was tested
            against, and what was rejected while writing it</p>
            <pre>${esc(JSON.stringify(r.measured, null, 2))}</pre>` : ""}
          <p class="frag-label">match — ${esc(r.file)}</p>
          <pre>${esc(JSON.stringify(r.match, null, 2))}</pre>
          ${r.domain ? `<p class="frag-label">domain — the population this rule claims
            to govern; changes no routing</p>
            <pre>${esc(JSON.stringify(r.domain, null, 2))}</pre>` : ""}
        </div>
      </details>`;
    }).join("") +
    `<div class="note">Click any rule to read its description, the evidence it was
     measured against, and the match tree the engine evaluates. Sorted by precedence,
     highest first — the order the engine resolves them in.</div>`;
  } catch (err) { fail(el, err); }
}

// ── the counts identity, worked on this release's biggest rule ─────────────
// A named example beats the abstract identity: the reader can check it against the
// table directly above. Picked by largest gap, so it is always the rule whose numbers
// look most alarming.
function renderCounts(d) {
  const el = $("counts-body");
  try {
    const rt = d.routing || {};
    const won = {};
    (rt.by_rule || []).forEach((r) => {
      (won[r.rule_id] = won[r.rule_id] || {})[r.pile] = r.count;
    });
    const rows = (rt.evaluations || []).map((e) => {
      const w = won[e.spec_id];
      if (!w) return null;                       // shadow: it never wins anything
      const main = Object.entries(w).filter(([p]) => p !== "pending")
        .reduce((a, [, c]) => a + c, 0);
      const held = w.pending || 0;
      return { id: e.spec_id, matched: e.matched, won: main, held,
               outranked: e.matched - main - held };
    }).filter(Boolean).sort((a, b) => b.outranked - a.outranked);

    if (!rows.length) { el.innerHTML = ""; return; }
    const r = rows[0];
    el.innerHTML = `<b>Worked example, from this release.</b>
      <code>${esc(r.id)}</code> matched <b>${n(r.matched)}</b> works and won
      <b>${n(r.won)}</b>${r.held ? `, with ${n(r.held)} downgraded for having no
      abstract` : ""}. The other <b>${n(r.outranked)}</b> were claimed by a rule sitting
      above it — so every work it matched is accounted for:
      ${n(r.matched)} = ${n(r.won)} + ${n(r.held)} + ${n(r.outranked)}.`;
  } catch (err) { fail(el, err); }
}

// ── pending ─────────────────────────────────────────────────────────────────
const PENDING_MEANING = {
  no_filter_matched: "No rule in the book matched this work at all. Not a rejection — nothing has judged it yet. This is the bulk of the pool, and narrowing it is what adding new rules does.",
  no_text: "A rule that would have sent this work for screening matched, but the work has no abstract to screen. Held rather than discarded: recoverable coverage, once a text overlay supplies the missing abstract.",
};

function renderPending(d) {
  const el = $("pending-body");
  try {
    const reasons = (d.routing && d.routing.pending_reasons) || {};
    const total = Object.values(reasons).reduce((a, b) => a + b, 0);
    el.className = "";
    if (!total) { el.innerHTML = provLine(d.store_provenance); return; }
    el.innerHTML = Object.entries(reasons).sort((a, b) => b[1] - a[1]).map(([k, c]) => `
      <div class="reason">
        <div class="reason-head">
          <code>${esc(k || "(blank)")}</code>
          <span class="reason-count">${n(c)}<span class="sub"> · ${
            pct(c, total)} of pending</span></span>
        </div>
        <p>${esc(PENDING_MEANING[k] || "")}</p>
      </div>`).join("") + `<div id="notext-body"></div>`;
    renderNoText();
  } catch (err) { fail(el, err); }
}

// The held works themselves. They exist ONLY as routing rows — a `pending/no_text`
// work never reached Stage 3, so it is in no CSV and Check cannot open it. What can be
// offered is the routing row: the work id, the rule that wanted it, and what that rule
// matched on. The id links to the OpenAlex record, which is where the title is.
async function renderNoText() {
  const el = $("notext-body");
  if (!el) return;
  try {
    const d = await getJSON("/api/stage2/no-text?limit=200");
    if (!d.rows || !d.rows.length) { el.innerHTML = provLine(d.provenance); return; }
    el.innerHTML = `<details class="changelog notext">
      <summary><span class="cg-open">Show</span><span class="cg-shut">Hide</span>
        the works being held <span class="cg-hint">— first ${n(d.rows.length)} of ${
          n(d.total)}, newest ids last</span></summary>
      <div class="note">These have no row in <code>extracted.csv</code> and none in any
      set-aside file — they never reached Stage 3, so Check has nothing to show. Each id
      links to its OpenAlex record; the <b>evidence</b> is the text the rule matched on,
      which for a textless work is always its title.</div>
      <div class="scroll"><table class="doc">
        <thead><tr><th>Work</th><th>The rule that wanted it</th>
          <th>What it matched</th></tr></thead>
        <tbody>${d.rows.map((r) => `
          <tr><td class="mono"><a href="https://openalex.org/W${r.work_id}"
                target="_blank" rel="noopener">W${r.work_id}</a></td>
              <td class="mono">${esc(r.rule_id)}</td>
              <td>${esc(r.evidence)}</td></tr>`).join("")}
        </tbody></table></div>
    </details>`;
  } catch (err) { fail(el, err); }
}

// ── domains ─────────────────────────────────────────────────────────────────
function renderDomains(d) {
  const el = $("domains-body");
  try {
    const rows = d.domains || [];
    el.className = "";
    if (!rows.length) {
      el.innerHTML = `<div class="note">No live rule in this bundle declares a
        <code>domain</code>, so there is nothing to compare.</div>`;
      return;
    }
    el.innerHTML = `<div class="scroll"><table class="doc">
      <thead><tr><th>Rule</th><th>Pile</th><th>In its domain</th><th>It matched</th>
        <th>Missed and admitted anyway</th></tr></thead>
      <tbody>${rows.map((r) => `
        <tr><td class="mono">${esc(r.spec_id)}</td>
            <td>${r.pile ? pill(r.pile) : "—"}</td>
            <td class="num">${n(r.in_domain)}</td>
            <td class="num">${n(r.matched)}</td>
            <td class="num ${r.uncovered_admitted ? "warn" : ""}">${
              n(r.uncovered_admitted)}</td></tr>`).join("")}
      </tbody></table></div>`;
  } catch (err) { fail(el, err); }
}

// ── the screen ──────────────────────────────────────────────────────────────
function renderScreen(d) {
  const el = $("screen-body");
  const gateEl = $("gate-body");
  const cheapEl = $("cheap-body");
  try {
    const s = d.screen || {};
    el.className = "";
    el.innerHTML = `<div class="scroll"><table class="doc">
      <thead><tr><th>Voter</th><th>Model</th><th>Reasoning effort</th></tr></thead>
      <tbody>${(s.voters || []).map((v) => `
        <tr><td class="num">${v.slot}</td>
            <td class="mono">${esc(v.model)}</td>
            <td class="mono">${esc(v.effort || "—")}</td></tr>`).join("")}
      </tbody></table></div>
      <div class="note">The effort is part of the cache key and is load-bearing, not a
      detail: at effort <code>none</code> the DeepSeek voter discarded 7 settled
      positives. Two call sites may name the same model, so each passes its own value —
      and the two settings never share a cache entry.</div>
      <div class="stats">
        <div class="stat"><span class="k">Classify prompt</span>
          <span class="v mono small">${esc(s.prompt_version || "—")}</span>
          <span class="s">edit it and every work becomes claimable again</span></div>
        <div class="stat"><span class="k">Workers</span>
          <span class="v">${s.workers ?? "—"}</span>
          <span class="s">works judged in parallel per run</span></div>
      </div>
      <p>Each voter answers the same fixed schema: a <code>classification</code>
      (replication · reproduction · both · none · unclear), a boolean
      <code>confident</code>, a multi-select <code>categories</code>, an
      <code>evidence_quote</code> and its <code>reasoning</code>. The cache holds
      <strong>one entry per vote</strong>, keyed on the prompt version and that voter's
      model-at-effort — so swapping one voter re-buys exactly that voter's answers
      while the other's stay cache hits.</p>`;

    gateEl.className = "";
    gateEl.innerHTML = `<div class="gaterules">
      <div class="gaterule drop"><span class="gk">discard</span>
        <p>All votes say <code>none</code>, at any confidence. The work becomes
        <code>not_a_replication</code>.</p></div>
      <div class="gaterule keep"><span class="gk">proceed</span>
        <p>Everything else — including a confident split, and a lone confident
        <code>none</code> against one other vote.</p></div>
      <div class="gaterule hold"><span class="gk">no decision</span>
        <p>Fewer than two votes answered. One vote → <code>target_pending</code>, the
        re-run decides; zero → <code>api_error</code>. An incomplete screen is never a
        verdict, but each vote that did answer is cached on its own key, so the re-run
        buys only the gap.</p></div>
    </div>
    <div class="note">Unanimity is the whole rule: <b>no single voter discards
    alone</b>, whatever its confidence. An earlier gate also discarded on one confident
    <code>none</code>, which leaned on that voter's calibration — a per-model property a
    voter swap silently changes. Unanimity costs about one extra pass-through per 70
    hard negatives, and false inclusions are cheap where false discards are not.</div>`;

    cheapEl.className = "";
    cheapEl.innerHTML = `<div class="scroll"><table class="doc">
      <thead><tr><th>Cheap voter</th><th>Model</th></tr></thead>
      <tbody>${(s.cheap_voters || []).map((v) => `
        <tr><td class="num">${v.slot}</td>
            <td class="mono">${esc(v.model)}</td></tr>`).join("")}
      </tbody></table></div>
      <div class="note">Voter 2 is asked only when voter 1 said "no": once the row can
      no longer be discarded, a second opinion changes nothing. There is no global
      on/off switch, deliberately — a flag would apply the cheap gate to rows the rule
      book routed to the expensive tier.</div>`;
  } catch (err) {
    [el, gateEl, cheapEl].forEach((e) => fail(e, err));
  }
}

// ── hand-off ────────────────────────────────────────────────────────────────
const SCREEN_COL_NOTE = {
  screen_verdict: "The gate's outcome — what decided whether this row travels at all.",
  screen_record_type: "replication or reproduction; becomes the row's `type` and picks the outcome vocabulary.",
  screen_categories: "The union of both voters' categories. |-joined multi-select — match by substring, never equality.",
  screen_votes: "Each voter's classification and confidence. Carried in full because the pre-PDF title-search step is gated on both voters qualifying AND being confident — a summary of the gate is not enough.",
  screen_evidence: "The quote a voter based its answer on.",
  screen_reasoning: "The voter's stated reasoning.",
};

function renderHandoff(d) {
  const el = $("handoff-body");
  try {
    const h = d.handoff || {};
    el.className = "";
    el.innerHTML = `<div class="paths"><div class="path">
        <h4>How a screened work reaches Stage 3</h4>
        <p>The extract tier calls <code>decisions()</code> for the works a live,
        current-generation screen admitted, and rebuilds each row from the pool. The
        piles are met in this order — ${(h.piles || []).map((p) => pill(p)).join(" then ")}
        — expensive first, so a <code>--limit</code>ed run works through the strongest
        signal before the murky residue. Within a pile the order is the pool's.</p>
      </div>
      <div class="path">
        <h4>The optional snapshot</h4>
        <span class="loc">python -m filter.engine export-csv --out &lt;file&gt;</span>
        <p>Writes a release's screened rows to a CSV you name. Nothing in the standard
        flow reads it — it exists for when a person wants a readable record of what a
        release admitted. <code>--out</code> is required on purpose: a default name is
        how a derived file becomes a fixture that quietly goes stale.</p>
      </div></div>
      <h3>What travels on the row</h3>
      <p>The screen runs in one place only. What Stage 3 needs is written onto the row:</p>
      <div class="scroll"><table class="doc">
        <thead><tr><th>Column</th><th>What it carries</th></tr></thead>
        <tbody>${(h.screen_cols || []).map((c) => `
          <tr><td class="mono">${esc(c)}</td>
              <td>${esc(SCREEN_COL_NOTE[c] || "")}</td></tr>`).join("")}
        </tbody></table></div>
      <div class="note">Stage 3 never votes — structurally: <code>classify_replication</code>
      is not imported into <code>extract/run_extract.py</code> at all. A row whose
      <code>screen_verdict</code> is blank is written <code>target_pending</code>, and
      the extract tier's worklist never offers one.</div>`;
  } catch (err) { fail(el, err); }
}

// ── release ─────────────────────────────────────────────────────────────────
const INPUT_NOTE = {
  pool_manifest_hash: "The survivor pool — its recorded gate plus every parquet's name, size and row count.",
  overlay_hash: "The frozen text overlay, or null when there is none.",
  bundle_hash: "Every spec in the rule book, plus conventions.json, hashed whole.",
  engine_version: "The routing engine itself.",
  alias_release: "The OpenAlex work-id alias map that canonicalises merged ids.",
  schema_version: "The CSV column contract between stages.",
};

function renderRelease(d) {
  const el = $("release-body");
  try {
    const r = d.release;
    el.className = "";
    if (!r) { el.innerHTML = provLine(d.store_provenance); return; }
    const inputs = r.inputs || {};
    el.innerHTML = `<div class="stats">
        <div class="stat"><span class="k">Release on this machine</span>
          <span class="v mono small">${esc(r.id.slice(0, 12))}</span>
          <span class="s">${esc(r.created_at || "")}</span></div>
      </div>
      <div class="scroll"><table class="doc">
        <thead><tr><th>Input</th><th>Value</th><th>What it names</th></tr></thead>
        <tbody>${Object.keys(INPUT_NOTE).map((k) => `
          <tr><td class="mono">${esc(k)}</td>
              <td class="mono">${inputs[k] ? esc(String(inputs[k]).slice(0, 12)) : "—"}</td>
              <td>${esc(INPUT_NOTE[k])}</td></tr>`).join("")}
        </tbody></table></div>
      ${provLine(d.store_provenance)}`;
  } catch (err) { fail(el, err); }
}

// ── commands ────────────────────────────────────────────────────────────────
const COMMANDS = [
  [".venv/bin/python -m filter.engine route",
   "Route the whole pool into piles. Recomputes from the pool and the specs, and " +
   "mints a release id from the six inputs."],
  [".venv/bin/python -m filter.engine status",
   "Pile counts for the current release, plus which rules are inert."],
  [".venv/bin/python -m filter.engine diagnose",
   "The checks a rule must pass to be trusted — overlap, inertness, domain coverage."],
  [".venv/bin/python -m filter.engine screen --tier screen_expensive",
   "DRY RUN. Prints how many rows, the token-length distribution and what it would " +
   "cost. Nothing is claimed, fetched or spent without --run."],
  [".venv/bin/python -m filter.engine screen --tier screen_expensive --run",
   "The real thing: claims a batch, asks both voters, writes a permanent verdict per work."],
  [".venv/bin/python -m filter.engine export-csv --out <file>",
   "A release's screened rows as an ad-hoc record CSV."],
  [".venv/bin/python -m filter.engine specs",
   "Every spec the engine loads, with its precedence and whether it is shadow."],
];

function renderCommands() {
  $("commands-body").innerHTML = `<div class="cmdlist">${COMMANDS.map(([c, why]) =>
    `<div class="cmdrow"><code>${esc(c)}</code><p>${esc(why)}</p></div>`).join("")}</div>`;
}

renderCommands();
trackSections();
renderIssues("stage-2");
renderPrompts("prompts-body",
  ["build_classify_prompt", "build_prescreen_prompt"],
  {
    build_classify_prompt: "The front-door screen. Both voters answer this same prompt " +
      "and the same v3.2 schema; the gate reads their two answers. It is in the " +
      "screening generation fingerprint, so editing it makes every work claimable again.",
    build_prescreen_prompt: "The cheap discard-only tier's one question. Dormant — all " +
      "three screen_cheap specs are shadow, so no live row reaches it.",
  });
getJSON("/api/stage2").then((d) => {
  // The verdicts are a separate, slow read. Fire it now, redraw the funnel when it
  // lands, and never let it block anything else on the page.
  getJSON("/api/stage2/verdicts")
    .then((r) => renderFunnel(d, r.verdicts, r.provenance))
    .catch((err) => renderFunnel(d, null,
      { state: "absent", reason: `the verdicts request failed: ${err.message}` }));

  renderPipelineMap("map-body", {
    snapshot: {},
    poolTotals: d.pool && d.pool.total ? { total: d.pool.total } : null,
    hereIds: ["routed", "screened"], hereLabel: "Stage 2",
  });
  renderFunnel(d, null, null); renderPiles(d); renderRulebook(d); renderPending(d);
  renderDomains(d); renderScreen(d); renderHandoff(d); renderRelease(d);
  renderCounts(d);
}).catch((err) => {
  ["map-body", "funnel-body", "piles-body", "rulebook-body", "pending-body",
   "domains-body", "screen-body", "gate-body", "cheap-body", "handoff-body",
   "release-body"].forEach((id) => { if ($(id)) fail($(id), err); });
});
