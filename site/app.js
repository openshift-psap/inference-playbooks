// AI Hub Model Overview — catalog renderer
// Vanilla JS, no framework, no CDN, runs from file://

let CATALOG = null;
let state = {
  modelId: null,
  stackId: null,
  profileId: null,
  hwId: null,
  topoId: null,
  tab: 'config',
  copiedId: null
};

const esc = s => String(s ?? '').replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');

function currentModel() {
  return CATALOG.models.find(m => m.id === state.modelId) || CATALOG.models[0];
}

function viewKey() {
  return [state.stackId, state.hwId, state.topoId, state.profileId].join('|');
}

function currentView(m) {
  const key = viewKey();
  return m.views[key] || { blocked: true, reason: 'This combination is not available.', header_specs: [] };
}

function setState(patch) {
  state = { ...state, ...patch };
  render();
}

function wireDelegation() {
  document.getElementById('app').addEventListener('click', e => {
    const el = e.target.closest('[data-action]');
    if (!el) return;
    e.preventDefault();
    e.stopPropagation();
    const action = el.dataset.action;
    const id = el.dataset.id;
    const actions = {
      selectStack: () => setState({ stackId: id }),
      selectHardware: () => setState({ hwId: id }),
      selectTopology: () => setState({ topoId: id }),
      selectProfile: () => setState({ profileId: id }),
      selectTab: () => { setState({ tab: id }); window.scrollTo({ top: 0, behavior: 'smooth' }); },
      copy: () => copy(id),
      download: () => download(id),
      noop: () => {}
    };
    (actions[action] || actions.noop)();
  });
}

function copy(drawerId) {
  const m = currentModel();
  const view = currentView(m);
  let body = '';

  // Resolve drawer body by id
  if (drawerId === 'vllm-serve') {
    body = view.configure?.vllm_serve || '';
  } else if (drawerId === 'manifest') {
    body = view.configure?.manifest?.body || '';
  } else if (drawerId === 'bench') {
    body = view.benchmark?.harness_drawer?.code || '';
  }

  const text = String(body).replace(/<[^>]+>/g, '');
  const done = () => {
    setState({ copiedId: drawerId });
    setTimeout(() => {
      if (state.copiedId === drawerId) setState({ copiedId: null });
    }, 1400);
  };

  if (navigator.clipboard && navigator.clipboard.writeText) {
    navigator.clipboard.writeText(text).then(done).catch(done);
  } else {
    done();
  }
}

function download(fileKey) {
  const view = currentView(currentModel());
  const f = (view.files || {})[fileKey];
  if (!f) return;
  const blob = new Blob([f.body], { type: 'text/plain;charset=utf-8' });
  const url = URL.createObjectURL(blob);
  const a = document.createElement('a');
  a.href = url;
  a.download = f.name;
  document.body.appendChild(a);
  a.click();
  a.remove();
  setTimeout(() => URL.revokeObjectURL(url), 2000);
}

function renderBreadcrumb(m) {
  return `<div class="crumb">
    <a href="#" style="color:#6A6E73">AI Hub</a> /
    <a href="#" style="color:#6A6E73">Models</a> /
    <a href="#" style="color:#6A6E73">Catalog</a> /
    <span style="color:#151515">${esc(m.name)}</span>
  </div>`;
}

function renderModelCard(m) {
  return `<div class="model-card">
    <div style="width:56px;height:56px;border-radius:8px;background:${esc(m.icon_bg)};display:flex;align-items:center;justify-content:center;color:#fff;font-family:'Red Hat Display',sans-serif;font-weight:700;font-size:22px;flex-shrink:0">${esc(m.icon_letter)}</div>
    <div style="flex:1;min-width:280px">
      <div style="display:flex;align-items:center;gap:10px;flex-wrap:wrap">
        <h1 style="font-family:'Red Hat Display',sans-serif;font-size:24px;font-weight:700;margin:0;white-space:nowrap">${esc(m.name)}</h1>
        ${m.validated ? '<span class="pill--validated">✓ Maturity: validated</span>' : ''}
      </div>
      <div style="font-size:13px;color:#6A6E73;margin:6px 0 12px">Provided by ${esc(m.provider)} · Red Hat AI Validated Models</div>
    </div>
  </div>`;
}

function renderSelectors(m, view) {
  const stacks = m.selectors.stacks || [];
  const groups = {};
  stacks.forEach(s => {
    const g = s.group || 'Other';
    if (!groups[g]) groups[g] = [];
    groups[g].push(s);
  });

  // Explicit group order (Standalone before Platform); any other groups follow in stable order.
  const GROUP_ORDER = ['Standalone', 'Platform'];
  const orderedGroups = [
    ...GROUP_ORDER.filter(g => groups[g]),
    ...Object.keys(groups).filter(g => !GROUP_ORDER.includes(g))
  ];
  const stackHtml = orderedGroups.map(g => {
    const items = groups[g].map(s => {
      const active = s.id === state.stackId;
      const blocked = s.blocked === true;
      const borderStyle = blocked ? 'dashed' : 'solid';
      const bg = active ? '#151515' : '#fff';
      const color = active ? '#fff' : (blocked ? '#8A8D90' : '#3C3F42');
      const borderColor = active ? '#151515' : '#D2D2D2';
      const subColor = active ? '#E8E8E8' : (blocked ? '#A8AAAD' : '#6A6E73');
      return `<button class="btn${active ? ' btn--active' : ''}${blocked ? ' btn--blocked' : ''}"
        data-action="${blocked ? 'noop' : 'selectStack'}" data-id="${esc(s.id)}"
        style="background:${bg};color:${color};border:1px ${borderStyle} ${borderColor};flex-direction:column;align-items:flex-start;gap:2px;text-align:left;${blocked ? 'cursor:not-allowed;' : ''}">
        <span style="display:flex;align-items:center;gap:6px">${esc(s.label)}</span>
        <span style="font-size:11.5px;font-weight:400;white-space:nowrap;color:${subColor}">${esc(s.sub || '')}</span>
      </button>`;
    }).join('');
    return `<div style="display:flex;flex-direction:column;gap:6px">
      <span style="font-family:monospace;font-size:9.5px;letter-spacing:.1em;text-transform:uppercase;color:#8A8D90">${esc(g)}</span>
      <div style="display:flex;gap:8px;flex-wrap:nowrap">${items}</div>
    </div>`;
  }).join('');

  const hwHtml = (m.selectors.hardware || []).map(h => {
    const active = h.id === state.hwId;
    const rec = h.recommended && active;
    return `<button class="btn${active ? ' btn--active' : ''}" data-action="selectHardware" data-id="${esc(h.id)}">
      ${esc(h.label)}${h.recommended ? '<span style="font-family:monospace;font-size:9.5px;letter-spacing:.08em;text-transform:uppercase;color:' + (rec ? '#85C957' : '#6A6E73') + ';font-weight:500;margin-left:6px">Recommended</span>' : ''}
    </button>`;
  }).join('');

  const topoHtml = (m.selectors.topology || []).map(t => {
    const key = [state.stackId, state.hwId, t.id, state.profileId].join('|');
    const testView = m.views[key] || {};
    const blocked = testView.blocked === true;
    const active = t.id === state.topoId;
    return `<button class="btn${active ? ' btn--active' : ''}${blocked ? ' btn--blocked' : ''}"
      data-action="${blocked ? 'noop' : 'selectTopology'}" data-id="${esc(t.id)}"
      ${blocked ? 'disabled' : ''}>
      ${esc(t.label)}
    </button>`;
  }).join('');

  const profileHtml = (m.selectors.profiles || []).map(p => {
    const active = p.id === state.profileId;
    return `<button class="btn${active ? ' btn--active' : ''}" data-action="selectProfile" data-id="${esc(p.id)}" style="text-align:left;flex-direction:column;align-items:flex-start;gap:2px">
      <span>${esc(p.label)}</span>
      ${p.sub ? '<span style="font-size:11.5px;font-weight:400;color:#6A6E73">' + esc(p.sub) + '</span>' : ''}
    </button>`;
  }).join('');

  const specsHtml = (view.header_specs || []).length > 0 ? `<div class="spec-grid">
    ${(view.header_specs || []).map(sp => `<div class="spec">
      <dt>${esc(sp.k)}</dt>
      <dd${sp.mono ? ' style="font-family:monospace"' : ''}>${esc(sp.v)}</dd>
    </div>`).join('')}
  </div>` : '';

  return `<div class="selectors">
    <div class="sel-row" style="border-bottom:1px solid #F0F0F0">
      <span class="sel-label">Stack</span>
      <div style="display:flex;gap:20px;flex-wrap:wrap;flex:1;min-width:0">${stackHtml}</div>
    </div>
    <div class="sel-row">
      <span class="sel-label">Hardware</span>
      <div style="display:flex;gap:8px;flex-wrap:wrap">${hwHtml}</div>
    </div>
    <div class="sel-row">
      <span class="sel-label">Scope</span>
      <div style="display:flex;gap:8px;flex-wrap:wrap">${topoHtml}</div>
    </div>
    <div class="sel-row">
      <span class="sel-label">Workload</span>
      <div style="display:flex;gap:8px;flex-wrap:wrap">${profileHtml}</div>
    </div>
    ${specsHtml}
  </div>`;
}

function renderTabs() {
  const tabs = [
    { id: 'start', label: 'Quick start' },
    { id: 'config', label: 'Configure' },
    { id: 'bench', label: 'Benchmark' },
    { id: 'airgap', label: 'Disconnected' },
    { id: 'adv', label: 'Advanced' }
  ];
  return `<div class="tabs">
    ${tabs.map(t => `<button class="tab${state.tab === t.id ? ' tab--active' : ''}" data-action="selectTab" data-id="${t.id}">
      ${esc(t.label)}
    </button>`).join('')}
  </div>`;
}

function renderStart(view) {
  if (view.blocked) {
    return `<div class="banner--pending">${esc(view.reason)}</div>`;
  }
  const isEmpty = !view.quick_start || Object.keys(view.quick_start).length === 0;
  if (isEmpty) {
    return `<div class="banner">No data for this combination yet.</div>`;
  }
  // If we had data, render steps here (currently unreachable given current catalog)
  return `<div class="banner">Quick start content would appear here.</div>`;
}

function renderConfigure(view) {
  if (view.blocked) {
    return `<div class="banner--pending">${esc(view.reason)}</div>`;
  }
  const cfg = view.configure;
  if (!cfg || Object.keys(cfg).length === 0) {
    return `<div class="banner">No data for this combination yet.</div>`;
  }

  const imageTable = (cfg.image_table || []).length > 0 ? `
    <h2 style="font-family:'Red Hat Display',sans-serif;font-size:18px;margin:0 0 12px">Container image</h2>
    <table class="rht">
      <thead><tr>
        <th>Use</th><th>Reference</th><th>Status</th>
      </tr></thead>
      <tbody>
        ${(cfg.image_table || []).map(r => `<tr>
          <td>${esc(r[0])}</td>
          <td style="font-family:monospace;font-size:12px">${esc(r[1])}</td>
          <td>${esc(r[2])}</td>
        </tr>`).join('')}
      </tbody>
    </table>` : '';

  const artifactTable = (cfg.artifact_table || []).length > 0 ? `
    <h2 style="font-family:'Red Hat Display',sans-serif;font-size:18px;margin:0 0 12px">Model artifact</h2>
    <table class="rht">
      <thead><tr>
        <th>Artifact</th><th>Size</th><th>Revision</th><th>Use</th>
      </tr></thead>
      <tbody>
        ${(cfg.artifact_table || []).map(r => `<tr>
          <td style="font-family:monospace;font-size:12.5px">${esc(r[0])}</td>
          <td style="font-family:monospace;font-size:12.5px">${esc(r[1])}</td>
          <td style="font-family:monospace;font-size:11px">${esc(r[2])}</td>
          <td style="font-size:13px">${esc(r[3])}</td>
        </tr>`).join('')}
      </tbody>
    </table>` : '';

  const flagsSection = (cfg.flags_rows || []).length > 0 ? `
    <h2 style="font-family:'Red Hat Display',sans-serif;font-size:18px;margin:0 0 12px">vLLM arguments</h2>
    ${/* trusted-HTML allow-list: flags_lede intentionally embeds <code>, kept raw */ ''}
    ${cfg.flags_lede ? '<p style="font-size:14px;color:#3C3F42;margin:0 0 12px">' + cfg.flags_lede + '</p>' : ''}
    ${cfg.vllm_serve ? `<details open class="drawer">
      <summary class="drawer__summary">
        <span style="font-family:monospace;font-size:12.5px;font-weight:600;white-space:nowrap">vllm serve</span>
        <span style="font-size:12px;color:#6A6E73;flex:1">Standalone command</span>
        <button data-action="copy" data-id="vllm-serve" style="font-family:monospace;font-size:10px;text-transform:uppercase;font-weight:600;color:${state.copiedId === 'vllm-serve' ? '#3D7317' : '#D2D2D2'};background:#fff;border:1px solid ${state.copiedId === 'vllm-serve' ? '#3D7317' : '#D2D2D2'};border-radius:3px;padding:4px 8px;cursor:pointer">${state.copiedId === 'vllm-serve' ? 'Copied' : 'Copy'}</button>
      </summary>
      <pre class="drawer__code" style="margin:0;white-space:pre">${esc(cfg.vllm_serve)}</pre>
    </details>` : ''}
    <table class="rht">
      <thead><tr>
        <th>Flag</th><th>Value</th><th>Why</th>
      </tr></thead>
      <tbody>
        ${(cfg.flags_rows || []).map(f => `<tr>
          <td style="font-family:monospace;font-size:12px">${esc(f.flag)}</td>
          <td style="font-family:monospace;font-size:12px">${esc(f.value)}</td>
          <td style="font-size:13px">${esc(f.why)}</td>
        </tr>`).join('')}
      </tbody>
    </table>
    ${/* trusted-HTML allow-list: arg_note intentionally embeds <code>, kept raw */ ''}
    ${cfg.arg_note ? '<div style="background:#FAFAFA;padding:10px 14px;font-size:13px;color:#3C3F42;margin:0 0 20px">' + cfg.arg_note + '</div>' : ''}` : '';

  const manifestSection = cfg.manifest ? `
    <h2 style="font-family:'Red Hat Display',sans-serif;font-size:18px;margin:0 0 12px">Manifest</h2>
    <details open class="drawer">
      <summary class="drawer__summary">
        <span style="font-family:monospace;font-size:12.5px;font-weight:600;white-space:nowrap">${esc(cfg.manifest.name)}</span>
        <span style="font-size:12px;color:#6A6E73;flex:1">RHOAI LLMInferenceService</span>
        <button data-action="copy" data-id="manifest" style="font-family:monospace;font-size:10px;text-transform:uppercase;font-weight:600;color:${state.copiedId === 'manifest' ? '#3D7317' : '#D2D2D2'};background:#fff;border:1px solid ${state.copiedId === 'manifest' ? '#3D7317' : '#D2D2D2'};border-radius:3px;padding:4px 8px;cursor:pointer">${state.copiedId === 'manifest' ? 'Copied' : 'Copy'}</button>
      </summary>
      <pre class="drawer__code" style="margin:0;white-space:pre">${esc(cfg.manifest.body)}</pre>
    </details>` : '';

  return `<div style="background:#fff;border:1px solid #D2D2D2;border-radius:6px;padding:24px 28px;max-width:900px">
    ${imageTable}
    ${artifactTable}
    ${flagsSection}
    ${manifestSection}
  </div>`;
}

function renderBenchmark(view) {
  if (view.blocked) {
    return `<div class="banner--pending">${esc(view.reason)}</div>`;
  }
  const bench = view.benchmark;
  if (!bench || Object.keys(bench).length === 0) {
    return `<div class="banner">No data for this combination yet.</div>`;
  }

  const provenanceHtml = bench.provenance ? `<div class="banner" style="margin-bottom:20px">${esc(bench.provenance)}</div>` : '';

  const harnessDrawerHtml = bench.harness_drawer && bench.harness_drawer.code ? `
    <h2 style="font-family:'Red Hat Display',sans-serif;font-size:18px;margin:0 0 10px">Harness</h2>
    ${bench.harness_lede ? '<p style="font-size:14px;color:#3C3F42;margin:0 0 12px;max-width:70ch">' + esc(bench.harness_lede) + '</p>' : ''}
    <details ${bench.harness_drawer.open ? 'open' : ''} class="drawer" style="margin-bottom:20px">
      <summary class="drawer__summary">
        <span style="font-family:monospace;font-size:12.5px;font-weight:600;white-space:nowrap">${esc(bench.harness_drawer.name)}</span>
        <span style="font-size:12px;color:#6A6E73;flex:1">${esc(bench.harness_drawer.meta || '')}</span>
        <button data-action="copy" data-id="bench" style="font-family:monospace;font-size:10px;text-transform:uppercase;font-weight:600;color:${state.copiedId === 'bench' ? '#3D7317' : '#D2D2D2'};background:#fff;border:1px solid ${state.copiedId === 'bench' ? '#3D7317' : '#D2D2D2'};border-radius:3px;padding:4px 8px;cursor:pointer">${state.copiedId === 'bench' ? 'Copied' : 'Copy'}</button>
      </summary>
      <pre class="drawer__code" style="margin:0;white-space:pre">${esc(bench.harness_drawer.code)}</pre>
    </details>` : '';

  const rowsHtml = (bench.rows || []).length > 0 ? `
    <h2 style="font-family:'Red Hat Display',sans-serif;font-size:18px;margin:0 0 12px">Measured</h2>
    <table class="rht">
      <thead><tr>
        <th>Load</th><th>TTFT</th><th>ITL</th><th>Throughput</th><th>Cache hit</th>
      </tr></thead>
      <tbody>
        ${(bench.rows || []).map(r => `<tr>
          <td style="font-size:12.5px">${esc(r.load || '')}</td>
          <td style="font-family:monospace;font-size:12px">${esc(r.ttft || '')}</td>
          <td style="font-family:monospace;font-size:12px">${esc(r.itl || '')}</td>
          <td style="font-family:monospace;font-size:12px">${esc(r.tput || '')}</td>
          <td style="font-family:monospace;font-size:12px">${esc(r.hit || '')}</td>
        </tr>`).join('')}
      </tbody>
    </table>` : '';

  const notesHtml = (bench.notes || []).length > 0 ? `
    <h2 style="font-family:'Red Hat Display',sans-serif;font-size:18px;margin:0 0 12px">What to hold onto</h2>
    <ul style="margin:0 0 20px;padding-left:20px;max-width:70ch;font-size:14px">
      ${(bench.notes || []).map(n => `<li style="margin-bottom:8px">${esc(n)}</li>`).join('')}
    </ul>` : '';

  const issuesHtml = (bench.issues || []).length > 0 ? `
    <h2 style="font-family:'Red Hat Display',sans-serif;font-size:18px;margin:0 0 12px">Known issues</h2>
    <table class="rht">
      <thead><tr>
        <th>Issue</th><th>Impact</th><th>Mitigation</th>
      </tr></thead>
      <tbody>
        ${(bench.issues || []).map(i => `<tr>
          <td>${esc(i[0] || '')}</td>
          <td>${esc(i[1] || '')}</td>
          <td>${esc(i[2] || '')}</td>
        </tr>`).join('')}
      </tbody>
    </table>` : '';

  return `<div style="background:#fff;border:1px solid #D2D2D2;border-radius:6px;padding:24px 28px">
    ${provenanceHtml}
    ${harnessDrawerHtml}
    ${rowsHtml}
    ${notesHtml}
    ${issuesHtml}
  </div>`;
}

function renderDisconnected(view) {
  if (view.blocked) {
    return `<div class="banner--pending">${esc(view.reason)}</div>`;
  }
  const disc = view.disconnected;
  if (!disc || Object.keys(disc).length === 0) {
    return `<div class="banner">No data for this combination yet.</div>`;
  }
  return `<div class="banner">Disconnected content would appear here.</div>`;
}

function renderAdvanced(view) {
  if (view.blocked) {
    return `<div class="banner--pending">${esc(view.reason)}</div>`;
  }
  const adv = view.advanced;
  if (!adv || Object.keys(adv).length === 0) {
    return `<div class="banner">No data for this combination yet.</div>`;
  }
  return `<div class="banner">Advanced content would appear here.</div>`;
}

function renderPanel(view) {
  let content = '';
  if (state.tab === 'start') content = renderStart(view);
  else if (state.tab === 'config') content = renderConfigure(view);
  else if (state.tab === 'bench') content = renderBenchmark(view);
  else if (state.tab === 'airgap') content = renderDisconnected(view);
  else if (state.tab === 'adv') content = renderAdvanced(view);

  return `<div class="panel">${content}</div>`;
}

function render() {
  if (!CATALOG || !state.modelId) return;
  const m = currentModel();
  const view = currentView(m);

  const html = `
    <div style="padding:24px 40px 0;flex:1;display:flex;flex-direction:column;max-width:1100px;width:100%;margin:0 auto">
      ${renderBreadcrumb(m)}
      ${renderModelCard(m)}
      ${renderSelectors(m, view)}
      ${renderTabs()}
      ${renderPanel(view)}
    </div>
  `;

  document.getElementById('app').innerHTML = html;
}

async function boot() {
  try {
    const response = await fetch('./catalog.json');
    CATALOG = await response.json();
    const wanted = new URLSearchParams(location.search).get('model');
    const m = CATALOG.models.find(x => x.id === wanted) || CATALOG.models[0];
    state.modelId = m.id;
    state.stackId = m.default.stack;
    state.hwId = m.default.hw;
    state.topoId = m.default.topo;
    state.profileId = m.default.profile;
    wireDelegation();
    render();
  } catch (err) {
    document.getElementById('app').innerHTML = `<div style="padding:40px;color:#A30000">Failed to load catalog: ${esc(err.message)}</div>`;
  }
}

document.addEventListener('DOMContentLoaded', boot);
