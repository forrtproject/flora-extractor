/*
 * render.js — Renders the result of /api/lookup (run_for_doi) in the Single DOI page.
 *
 * Called by disambiguation.html after a successful /api/lookup response.
 * Public API: renderResults(data)
 */

function esc(s) {
  return String(s == null ? '' : s).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;');
}

function trunc(s, n) { s = (s || ''); return s.length > n ? s.slice(0, n) + '…' : s; }

function pill(text, cls) {
  return `<span style="display:inline-flex;align-items:center;padding:3px 9px;border-radius:10px;
    font-size:11px;font-weight:600;white-space:nowrap;${cls}">${esc(text || '—')}</span>`;
}

function pillOutcome(v) {
  const styles = {
    success:      'background:#dcfce7;color:#166534',
    failure:      'background:#fee2e2;color:#991b1b',
    mixed:        'background:#fef3c7;color:#92400e',
    uninformative:'background:#e2e8f0;color:#334155',
    cannot_be_determined:'background:#e2e8f0;color:#334155',
    descriptive:  'background:#e2e8f0;color:#334155',
    pending:      'background:#e2e8f0;color:#334155',
  };
  return pill(v, styles[v] || 'background:#e2e8f0;color:#334155');
}

function pillMethod(v) {
  const styles = {
    llm_fulltext:       'background:#dbeafe;color:#1d4ed8',
    llm_abstract:       'background:#dbeafe;color:#1d4ed8',
    author_year_match:  'background:#dcfce7;color:#166534',
    target_pending:     'background:#e2e8f0;color:#334155',
    api_error:          'background:#fee2e2;color:#991b1b',
  };
  return pill(v, styles[v] || 'background:#e2e8f0;color:#334155');
}

function pillConf(v) {
  const styles = {
    high:   'background:#dcfce7;color:#166534',
    medium: 'background:#fef3c7;color:#92400e',
    low:    'background:#fee2e2;color:#991b1b',
  };
  return pill(v, styles[v] || 'background:#e2e8f0;color:#334155');
}

function section(title, cls, content) {
  return `
    <div style="background:#fff;border:1px solid #e2e8f0;border-radius:8px;
                padding:12px 14px;margin-bottom:10px;${cls||''}">
      <div style="display:flex;align-items:center;gap:8px;margin-bottom:8px">
        <span style="display:inline-flex;align-items:center;padding:3px 9px;border-radius:10px;
                     font-size:10px;font-weight:700;${title.style||'background:#e2e8f0;color:#334155'}">
          ${esc(title.text)}
        </span>
        ${title.sub ? `<span style="font-size:9.5px;font-weight:800;text-transform:uppercase;
          letter-spacing:.08em;color:#94a3b8">${esc(title.sub)}</span>` : ''}
      </div>
      ${content}
    </div>`;
}

function field(label, val, mono) {
  const style = mono ? 'font-family:monospace;font-size:10px;color:#2563eb' : 'font-size:11px;color:#334155';
  return `<div>
    <div style="font-size:9px;font-weight:700;text-transform:uppercase;letter-spacing:.06em;
                color:#94a3b8;margin-bottom:2px">${label}</div>
    <div style="${style}">${esc(val) || '<span style="color:#cbd5e1">—</span>'}</div>
  </div>`;
}

function grid(cols, children) {
  return `<div style="display:grid;grid-template-columns:${cols};gap:6px 16px;margin-bottom:8px">
    ${children.join('')}</div>`;
}

/* ── main render function ─────────────────────────────────────────────────── */
function renderResults(data) {
  const el = document.getElementById('results');
  el.style.display = 'block';
  document.getElementById('empty-state').style.display = 'none';

  /* normalise field names — run_for_doi uses resolved_* prefix */
  const doi_r        = data.doi_r        || '';
  const study_r      = data.study_r      || data.title_r || '';
  const abstract_r   = data.abstract_r   || '';
  const doi_o        = data.resolved_doi_o   || data.doi_o   || '';
  const title_o      = data.resolved_title_o || data.title_o || data.study_o || '';
  const year_o       = data.resolved_year_o  || data.year_o  || '';
  const authors_o    = data.resolved_author_o|| data.authors_o|| '';
  const link_method  = data.resolution_method|| data.link_method || '';
  const link_ev      = data.llm_evidence  || data.link_evidence  || '';
  const link_conf    = data.llm_confidence|| data.link_confidence || '';
  const outcome      = data.outcome       || '';
  const out_phrase   = data.outcome_quote || data.outcome_phrase  || '';
  const pdf_source   = data.pdf_source    || '';
  const pdf_url      = data.pdf_url       || '';
  const pdf_serve    = data.pdf_serve_url || '';
  const grobid_stat  = data.grobid_status || '';
  const refs         = data.grobid_refs   || [];
  const grobid_abs   = data.grobid_abstract || '';
  const grobid_intro = data.grobid_intro  || '';
  const candidates   = data.candidates    || [];
  const llm_prompt   = data.llm_prompt    || '';
  const llm_source   = data.llm_source    || '';

  const linkResolved = doi_o && link_method !== 'target_pending' && link_method !== 'api_error';

  let html = '';

  /* ── 1: Resolved Original ──────────────────────────────────────────────── */
  html += section(
    { text: 'RESOLVED — ORIGINAL STUDY',
      style: linkResolved ? 'background:#dcfce7;color:#166534' : 'background:#e2e8f0;color:#334155',
      sub: 'Stage 3 linking result' },
    linkResolved ? 'border-left:4px solid #059669' : '',
    linkResolved ? `
      <div style="font-size:14px;font-weight:700;color:#1d4ed8;line-height:1.4;margin-bottom:6px">
        ${esc(title_o)}</div>
      ${grid('repeat(4,1fr)', [
        field('Original DOI', doi_o, true),
        field('Year', year_o),
        field('Authors', trunc(authors_o, 60)),
        `<div>${field('Link Confidence', '')}${pillConf(link_conf)}</div>`,
      ])}
      <div style="display:flex;gap:6px;align-items:center;margin-bottom:8px">
        ${pillMethod(link_method)}
        <a href="https://doi.org/${esc(doi_o)}" target="_blank"
           style="font-size:11px;color:#2563eb;font-family:monospace">${esc(doi_o)} ↗</a>
      </div>
      ${link_ev ? `<div style="font-size:9px;font-weight:700;text-transform:uppercase;
            color:#94a3b8;margin-bottom:3px">EVIDENCE</div>
          <div style="background:#f0fdf4;border-left:3px solid #6ee7b7;padding:6px 10px;
                      font-size:11px;font-style:italic;color:#475569;border-radius:0 4px 4px 0">
            "${esc(link_ev)}"</div>` : ''}
    ` : `<div style="color:#94a3b8;font-size:11px">
      ${link_method === 'api_error' ? '⚠ Extraction failed' : 'Original study not resolved'}
    </div>`
  );

  /* ── 2: Outcome ────────────────────────────────────────────────────────── */
  if (outcome) {
    const outStyle = {success:'background:#dcfce7;color:#166534',failure:'background:#fee2e2;color:#991b1b',
      mixed:'background:#fef3c7;color:#92400e'}[outcome] || 'background:#e2e8f0;color:#334155';
    html += section(
      { text:'OUTCOME', style:outStyle, sub:'Replication result' },
      '',
      `<div style="display:flex;gap:8px;align-items:center;margin-bottom:6px">
        ${pillOutcome(outcome)}
      </div>
      ${out_phrase ? `<div style="font-size:11px;font-style:italic;color:#475569">
        "${esc(out_phrase)}"</div>` : ''}`
    );
  }

  /* ── 3: OpenAlex Candidates ────────────────────────────────────────────── */
  if (candidates.length > 0) {
    const rows = candidates.map((c, i) => {
      const hit = doi_o && c.doi && c.doi.toLowerCase() === doi_o.toLowerCase();
      return `<tr style="${hit ? 'background:#dcfce7;font-weight:600' : ''}">
        <td style="padding:4px 8px">${i + 1}</td>
        <td style="padding:4px 8px">${esc(c.title || c.study_o || '')}</td>
        <td style="padding:4px 8px">${esc(c.year || '')}</td>
        <td style="padding:4px 8px">${esc(
          Array.isArray(c.authors) ? c.authors[0] : (c.first_author || c.authors || '')
        )}</td>
        <td style="padding:4px 8px;font-family:monospace;font-size:9px;color:#2563eb">${esc(c.doi || '')}</td>
        <td style="padding:4px 8px;text-align:center">${c.year_exact === false ? '±1yr' : '✓'}</td>
      </tr>`;
    }).join('');

    html += section(
      { text:'OPENALEX', style:'background:#dbeafe;color:#1d4ed8', sub:`Re-query — ${candidates.length} candidates` },
      '',
      `<table style="width:100%;border-collapse:collapse;font-size:11px">
        <thead><tr style="background:#f8fafc">
          <th style="padding:4px 8px;text-align:left;font-size:9px;font-weight:700;color:#64748b">#</th>
          <th style="padding:4px 8px;text-align:left;font-size:9px;font-weight:700;color:#64748b">Title</th>
          <th style="padding:4px 8px;text-align:left;font-size:9px;font-weight:700;color:#64748b">Year</th>
          <th style="padding:4px 8px;text-align:left;font-size:9px;font-weight:700;color:#64748b">First Author</th>
          <th style="padding:4px 8px;text-align:left;font-size:9px;font-weight:700;color:#64748b">DOI</th>
          <th style="padding:4px 8px;text-align:left;font-size:9px;font-weight:700;color:#64748b">Year Exact?</th>
        </tr></thead>
        <tbody>${rows}</tbody>
      </table>`
    );
  }

  /* ── 4: PDF Acquisition ────────────────────────────────────────────────── */
  if (pdf_source || pdf_url) {
    html += section(
      { text:'PDF ACQUISITION', style:'background:#94a3b8;color:#fff', sub:'How full text was obtained' },
      '',
      grid('repeat(3,1fr)', [
        `<div>${field('Source', '')}${pill(pdf_source, 'background:#dcfce7;color:#166534')}</div>`,
        `<div style="grid-column:span 2">${field('URL', pdf_url, true)}</div>`,
      ]) +
      (pdf_serve ? `<div style="margin-top:6px">
        <a href="${esc(pdf_serve)}" target="_blank"
           style="display:inline-flex;align-items:center;gap:4px;padding:5px 10px;
                  background:#dc2626;color:#fff;border-radius:5px;font-size:11px;
                  font-weight:600;text-decoration:none">
          📄 Open PDF (cached)
        </a></div>` : '')
    );
  }

  /* ── 5: PDF Section Extraction ─────────────────────────────────────────── */
  if (grobid_stat || grobid_abs || grobid_intro || refs.length > 0) {
    let gContent = grid('repeat(3,1fr)', [
      `<div>${field('Status', '')}${pill(grobid_stat, grobid_stat==='success'?'background:#dcfce7;color:#166534':'background:#fef3c7;color:#92400e')}</div>`,
      field('References parsed', String(refs.length)),
      '',
    ]);

    if (grobid_abs) gContent += collap('Abstract (PDF extract)', esc(grobid_abs));
    if (grobid_intro) gContent += collap('Introduction (PDF extract)', esc(grobid_intro));

    if (refs.length > 0) {
      const refRows = refs.slice(0, 30).map((r, i) => `<tr>
        <td style="padding:3px 8px">${i + 1}</td>
        <td style="padding:3px 8px">${esc(Array.isArray(r.authors) ? r.authors.slice(0, 2).join(', ') : (r.authors || ''))}</td>
        <td style="padding:3px 8px">${esc(r.year)}</td>
        <td style="padding:3px 8px">${esc(r.title)}</td>
      </tr>`).join('');
      gContent += `
        <div style="font-size:9px;font-weight:700;text-transform:uppercase;color:#94a3b8;margin:8px 0 4px">
          Reference List (first ${Math.min(refs.length, 30)} of ${refs.length})</div>
        <table style="width:100%;border-collapse:collapse;font-size:11px">
          <thead><tr style="background:#f8fafc">
            <th style="padding:3px 8px;text-align:left;font-size:9px;color:#64748b">#</th>
            <th style="padding:3px 8px;text-align:left;font-size:9px;color:#64748b">Authors</th>
            <th style="padding:3px 8px;text-align:left;font-size:9px;color:#64748b">Year</th>
            <th style="padding:3px 8px;text-align:left;font-size:9px;color:#64748b">Title</th>
          </tr></thead>
          <tbody>${refRows}</tbody>
        </table>`;
    }

    html += section(
      { text:'PDF SECTION EXTRACTION', style:'background:#94a3b8;color:#fff',
        sub:'pdfminer + GROBID reference parsing' },
      '', gContent
    );
  }

  /* ── 6: LLM Prompt ─────────────────────────────────────────────────────── */
  if (llm_prompt) {
    html += section(
      { text:'LLM INPUT / OUTPUT', style:'background:#059669;color:#fff',
        sub: llm_source ? `resolved via ${llm_source}` : 'DOI resolution prompt' },
      'border-left:4px solid #059669',
      `<pre style="background:#0f172a;border-radius:6px;padding:12px 16px;
                   font-family:'Consolas','Fira Mono',monospace;font-size:12px;color:#cbd5e1;
                   line-height:1.65;white-space:pre-wrap;overflow-x:auto;
                   max-height:400px;overflow-y:auto;margin:0">${esc(llm_prompt)}</pre>`
    );
  }

  /* ── 7: Replication Paper Info ─────────────────────────────────────────── */
  html += section(
    { text:'REPLICATION PAPER', style:'background:#e2e8f0;color:#334155', sub:'Input from Stage 2' },
    '',
    `${field('DOI', doi_r, true)}
     ${study_r ? `<div style="margin-top:6px">${field('Title', study_r)}</div>` : ''}
     ${abstract_r ? `${collap('Abstract', esc(abstract_r))}` : ''}`
  );

  el.innerHTML = `<div style="max-width:960px;margin:0 auto;padding:0 16px 24px">${html}</div>`;
}

/* ── collapsible helper ───────────────────────────────────────────────────── */
let _collapN = 0;
function collap(label, content) {
  const id = 'collap-' + (_collapN++);
  return `
    <div style="border-top:1px solid #f1f5f9;margin-top:6px;padding-top:4px">
      <button onclick="toggleCollap('${id}')"
              style="display:flex;align-items:center;gap:5px;background:none;border:none;
                     cursor:pointer;font-size:11px;color:#334155;padding:3px 0;user-select:none">
        <span id="${id}-arr">▶</span> ${esc(label)}
      </button>
      <div id="${id}" style="display:none;padding:4px 0 2px 14px;font-size:11px;
                              color:#475569;line-height:1.7">${content}</div>
    </div>`;
}
function toggleCollap(id) {
  const el  = document.getElementById(id);
  const arr = document.getElementById(id + '-arr');
  const open = el.style.display !== 'none';
  el.style.display  = open ? 'none' : 'block';
  arr.textContent   = open ? '▶' : '▼';
}

/* ── copy debug summary ───────────────────────────────────────────────────── */
async function copyDebugToClipboard(data) {
  const lines = [
    `DOI (R):        ${data.doi_r || ''}`,
    `Resolved DOI:   ${data.resolved_doi_o || data.doi_o || ''}`,
    `Resolved Title: ${data.resolved_title_o || data.title_o || ''}`,
    `Link Method:    ${data.resolution_method || data.link_method || ''}`,
    `Link Conf:      ${data.llm_confidence || data.link_confidence || ''}`,
    `Outcome:        ${data.outcome || ''}`,
    ``,
    `LLM Evidence:   ${data.llm_evidence || data.link_evidence || ''}`,
    ``,
    `LLM Prompt:`,
    data.llm_prompt || '(not available)',
  ];
  try {
    await navigator.clipboard.writeText(lines.join('\n'));
    return true;
  } catch (e) {
    return false;
  }
}
