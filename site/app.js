// AI Hub Model Overview — v4 recipe catalog renderer.
// Retains the contributor shell, fonts, styles, selectors, tabs and drawers.
// Contributor prose is text, never executable HTML. Preview requires HTTP.
let CATALOG = null;
let state = { modelId: null, entryId: null, tab: 'config', filters: {} };
const app = document.getElementById('app');

function node(tag, text, className) {
  const element = document.createElement(tag);
  if (text !== undefined && text !== null) element.textContent = String(text);
  if (className) element.className = className;
  return element;
}
function currentModel() { return CATALOG.models.find(model => model.id === state.modelId); }
function modelEntries() { return CATALOG.entries.filter(entry => entry.model_id === state.modelId); }
function currentView() { return modelEntries().find(entry => entry.id === state.entryId); }
function platformKey(entry) { return `${entry.platform.stack} ${entry.platform.version}`; }
function workloadLabel(value) {
  return { 'guidellm-8k1k': '8k/1k', 'aiperf-agentx-128k': 'Agentic workload (128K)', 'aiperf-agentx-unlimited-context': 'Agentic workload (unlimited)' }[value] || value;
}
function recipeLabel(entry) {
  const accelerators = entry.hardware.data.accelerators;
  return `${workloadLabel(entry.workload_profile)} — ${entry.gpu_allocation.declared_gpus_per_replica ?? 'Unknown'} ${accelerators.model} GPUs per replica (${accelerators.count_per_node} per host) — ${entry.scope} — ${platformKey(entry)}`;
}
function isDisaggregated(entry) {
  return ['prefix', 'disaggregated', 'prefill-decode'].includes(entry.serving.router?.strategy);
}
function parallelismRows(parallelism) {
  const rows = [['Tensor parallelism (TP)', parallelism.tp > 1 ? `${parallelism.tp} GPUs per tensor-parallel group` : '1 GPU (no tensor sharding)']];
  if (parallelism.pp > 1) rows.push(['Pipeline parallelism (PP)', `${parallelism.pp} pipeline stages`]);
  if (parallelism.dp > 1) rows.push(['Data parallelism (DP)', `${parallelism.dp} data-parallel ranks`]);
  return rows;
}
function maturityBadge(maturity) {
  const style = { 'day-zero': 'pending', contributed: 'contributed', validated: 'verified', production: 'verified' }[maturity] || 'neutral';
  return node('span', maturity, `status-badge status-badge--${style}`);
}
function renderRecipeTechnical(entry) {
  const technical = node('details', null, 'recipe-technical');
  technical.append(node('summary', 'Technical recipe identity and source rationale'), node('p', `Recipe ID: ${entry.recipe_id}`), node('p', `Platform entry: ${entry.id} — ${platformKey(entry)}`), sourceLink(entry.source, 'Recipe source'));
  const profile = entry.notes?.profile || {};
  const rationale = [profile.label, profile.headline, entry.notes?.default_stance].filter(Boolean);
  if (rationale.length) {
    technical.append(node('p', 'Selection rationale — source prose, not a measured recommendation.'), sourceLink(entry.notes_source, 'Selection rationale source'));
    rationale.forEach(text => technical.append(node('p', text)));
  } else technical.append(node('p', 'No selection rationale supplied'));
  technical.append(table(['Technical detail', 'Value'], [
    ['Hardware profile', `${entry.hardware.path} revision ${entry.hardware.revision}`],
    ['Declared serving allocation (per replica)', `${entry.gpu_allocation.declared_gpus_per_replica} GPUs`],
    ['Image', entry.serving.image], ['Image kind', entry.serving.image_usage?.kind || 'Not classified'],
    ['Image notes', entry.serving.image_usage?.note || 'None supplied'],
    ['Optimization target (declared, not a measurement)', entry.optimization_intent], ['Deployment identifier', entry.deployment_mode]
  ]), sourceLink(entry.hardware.source, 'Hardware profile source'), node('p', entry.command_note));
  for (const spec of entry.specs) technical.append(node('p', `${spec.label}: ${spec.state === 'resolved' ? JSON.stringify(spec.value) : 'Unverified source note — inspect final artifacts'} (${spec.source})`));
  return technical;
}
function dimension(entry, key) {
  if (key === 'platform') return { vllm: 'vllm', rhoai: 'rhoai/rhaii', rhaii: 'rhoai/rhaii', 'llm-d': 'llm-d' }[entry.platform.stack] || null;
  if (key === 'version') return entry.platform.version;
  if (key === 'gpu_model') return String(entry.hardware.data.accelerators.model || '').toUpperCase();
  if (key === 'workload') return { 'guidellm-8k1k': '8k1k', 'aiperf-agentx-128k': 'agentic', 'aiperf-agentx-unlimited-context': 'agentic' }[entry.workload_profile] || null;
  return entry[key];
}
function matchingEntries() {
  return modelEntries().filter(entry => Object.entries(state.filters).every(([key, value]) => !value || dimension(entry, key) === value));
}
function updateUrl() {
  const url = new URL(location.href);
  url.searchParams.set('model', state.modelId);
  if (state.entryId) url.searchParams.set('entry', state.entryId);
  else url.searchParams.delete('entry');
  history.replaceState(null, '', url);
}
function chooseModel(id, requestedEntry = null) {
  state.modelId = CATALOG.models.some(model => model.id === id) ? id : CATALOG.models[0]?.id;
  state.filters = {};
  const entries = modelEntries();
  state.entryId = entries.find(entry => entry.id === requestedEntry)?.id || entries.find(entry => !entry.blocked)?.id || entries[0]?.id || null;
  state.tab = 'config';
}
function selectFilter(key, value) {
  state.filters[key] = value;
  if (key === 'platform') state.filters.version = '';
  // Filters are strict: an unavailable combination remains visible as no results.
  const matches = matchingEntries();
  if (!matches.some(entry => entry.id === state.entryId)) {
    state.entryId = matches.find(entry => !entry.blocked)?.id || matches[0]?.id || null;
  }
  updateUrl(); render();
}
function compareVersions(left, right) {
  // Display ordering only: keep every exact source identity, including opaque labels.
  const pattern = /^v?(\d+)\.(\d+)(?:\.(\d+))?(?:-ea(\d+))?$/i;
  const a = left.match(pattern), b = right.match(pattern);
  if (a && b) {
    for (const index of [1, 2, 3]) {
      const leftNumber = BigInt(a[index] || '0'), rightNumber = BigInt(b[index] || '0');
      if (leftNumber !== rightNumber) return leftNumber < rightNumber ? -1 : 1;
    }
    if (a[4] === undefined && b[4] !== undefined) return 1;
    if (a[4] !== undefined && b[4] === undefined) return -1;
    const leftEA = BigInt(a[4] || '0'), rightEA = BigInt(b[4] || '0');
    if (leftEA !== rightEA) return leftEA < rightEA ? -1 : 1;
  } else if (a || b) return a ? -1 : 1;
  return left < right ? -1 : left > right ? 1 : 0;
}
function filterOptions(key, label, options, disabled = false) {
  const group = node('fieldset', null, 'filter-options');
  group.disabled = disabled;
  group.dataset.filter = key;
  group.setAttribute('role', 'radiogroup'); group.setAttribute('aria-label', label);
  group.append(node('legend', label));
  for (const [value, text] of options) {
    const selected = (state.filters[key] || '') === value;
    const unavailable = key === 'platform' && value !== '' && !modelEntries().some(entry => dimension(entry, 'platform') === value);
    const option = node('label', null, `filter-option${selected ? ' filter-option--selected' : ''}${unavailable ? ' filter-option--unavailable' : ''}`);
    const radio = node('input');
    radio.type = 'radio'; radio.name = `catalog-filter-${key}`; radio.value = value; radio.checked = selected;
    radio.disabled = unavailable;
    if (unavailable) option.title = 'No recipes for this platform family and selected model.';
    radio.dataset.focusKey = `filter:${key}:${value}`;
    radio.addEventListener('change', () => { if (!radio.disabled && !group.disabled) selectFilter(key, radio.value); });
    option.append(radio, node('span', text)); group.append(option);
  }
  return group;
}
function sourceLink(source, label = 'Source') {
  if (!source) return node('span', 'Source unavailable');
  try {
    const url = new URL(source.url);
    if (url.protocol !== 'https:' || url.username || url.password) throw new Error('Unsafe URL');
    const link = node('a', label);
    link.href = url.href; link.rel = 'noopener noreferrer';
    return link;
  } catch (_) { return node('span', `${source.path || label} (local preview)`); }
}
function table(headers, rows) {
  const result = node('table', null, 'rht');
  const head = node('thead'), heading = node('tr'), body = node('tbody');
  headers.forEach(text => heading.append(node('th', text)));
  head.append(heading); result.append(head, body);
  for (const row of rows) {
    const tr = node('tr');
    row.forEach(value => tr.append(node('td', value === null || value === undefined ? 'Unknown' : typeof value === 'object' ? JSON.stringify(value) : value)));
    body.append(tr);
  }
  return result;
}
async function copy(body, button, entryId) {
  try {
    if (!navigator.clipboard?.writeText) throw new Error('Clipboard unavailable');
    await navigator.clipboard.writeText(body);
    if (button.isConnected && state.entryId === entryId) button.textContent = 'Copied';
  } catch (_) {
    if (button.isConnected) button.textContent = 'Copy failed — use Download';
  }
}
function download(file) {
  const url = URL.createObjectURL(new Blob([file.body], { type: 'text/plain;charset=utf-8' }));
  const anchor = node('a'); anchor.href = url; anchor.download = file.name;
  document.body.append(anchor); anchor.click(); anchor.remove();
  setTimeout(() => URL.revokeObjectURL(url), 2000);
}
function drawer(name, body, downloadable = true) {
  const details = node('details', null, 'drawer'); details.open = true;
  const summary = node('summary', null, 'drawer__summary'); summary.append(node('strong', name));
  const button = node('button', 'Copy'); button.type = 'button';
  const entryId = state.entryId;
  button.addEventListener('click', event => { event.preventDefault(); copy(body, button, entryId); });
  summary.append(button);
  if (downloadable) {
    const save = node('button', 'Download'); save.type = 'button';
    save.addEventListener('click', event => { event.preventDefault(); download({ name, body }); });
    summary.append(save);
  }
  const code = node('pre', body, 'drawer__code');
  details.append(summary, code); return details;
}
function renderModelCard(model, entry) {
  const card = node('div', null, 'model-card');
  const presentation = model.metadata.presentation || {};
  const icon = node('div', presentation.icon_letter || (model.name || model.id)[0] || '?');
  icon.style.backgroundColor = /^#[a-f0-9]{6}$/i.test(presentation.icon_bg || '') ? presentation.icon_bg : '#4B2E83';
  icon.style.color = '#fff'; icon.style.padding = '18px'; icon.style.borderRadius = '8px';
  const hfIdentity = typeof model.metadata.huggingface_id === 'string'
    ? model.metadata.huggingface_id.match(/^([A-Za-z0-9][A-Za-z0-9_-]*)\/([A-Za-z0-9][A-Za-z0-9._-]*)$/) : null;
  const content = node('div'), heading = node('h1');
  const modelName = model.name || model.id;
  heading.append(hfIdentity
    ? sourceLink({ url: `https://huggingface.co/${encodeURIComponent(hfIdentity[1])}/${encodeURIComponent(hfIdentity[2])}` }, modelName)
    : node('span', modelName));
  content.append(heading);
  const meta = node('div', null, 'model-meta');
  const organization = node('span', 'HF organization: ');
  organization.append(hfIdentity
    ? sourceLink({ url: `https://huggingface.co/${encodeURIComponent(hfIdentity[1])}` }, hfIdentity[1])
    : node('span', 'Unknown'));
  meta.append(organization);
  if (entry) {
    const maturity = node('span', 'Recipe maturity: ');
    maturity.append(maturityBadge(entry.maturity));
    meta.append(maturity);
    if (entry.validation.label) meta.append(node('span', entry.validation.label, 'pill--validated'));
    meta.append(node('span', `Engine: ${entry.engine.version || 'Unknown'} (${entry.engine.source || 'unresolved'})`));
  }
  content.append(meta);
  if (entry?.validation.qualification) content.append(node('p', `${entry.validation.qualification} Tested on ${entry.validation.tested_platform.stack} ${entry.validation.tested_platform.version}.`));
  card.append(icon, content); return card;
}
function renderSelectors() {
  const result = node('div', null, 'selectors');
  const models = node('fieldset', null, 'model-options');
  models.setAttribute('role', 'radiogroup');
  models.setAttribute('aria-label', 'Models');
  models.append(node('legend', 'Models'));
  for (const model of CATALOG.models) {
    const selected = model.id === state.modelId;
    const label = node('label', null, `model-option${selected ? ' model-option--selected' : ''}`);
    const radio = node('input');
    radio.type = 'radio'; radio.name = 'catalog-model'; radio.value = model.id;
    radio.checked = selected;
    radio.setAttribute('aria-label', model.name || model.id);
    radio.dataset.focusKey = `model:${model.id}`;
    radio.addEventListener('change', () => { chooseModel(radio.value); updateUrl(); render(); });
    label.append(radio, node('span', model.name || model.id, 'model-option__name'), node('span', model.id, 'model-option__id'));
    models.append(label);
  }
  result.append(models);
  result.append(node('p', 'Choose a workload, then hardware and deployment scope. Workload names describe source benchmark profiles, not maximum runtime guarantees.'));
  const entries = modelEntries();
  result.append(filterOptions('platform', 'Platform', [['', 'All'], ['vllm', 'vLLM'], ['rhoai/rhaii', 'RHOAI / RHAII'], ['llm-d', 'llm-d']]));
  const family = state.filters.platform || '';
  const versions = family ? [...new Set(entries.filter(entry => dimension(entry, 'platform') === family).map(entry => entry.platform.version))].sort(compareVersions) : [];
  result.append(filterOptions('version', 'Version', [['', 'All versions'], ...versions.map(value => [value, value])], !versions.length));
  if (family && !versions.length) result.append(node('p', 'No versions are available for this platform family and model.', 'platform-unavailable'));
  result.append(filterOptions('gpu_model', 'GPU model', [['', 'All'], ...['B300', 'B200', 'H200', 'H100'].map(value => [value, value])]));
  const pair = node('div', null, 'filter-pair');
  pair.append(filterOptions('scope', 'Scope', [['', 'All'], ['single-node', 'Single-node'], ['multi-node', 'Multi-node']]),
    filterOptions('workload', 'Workload', [['', 'All'], ['8k1k', '8k/1k'], ['agentic', 'Agentic workload']]));
  result.append(pair);
  const matches = matchingEntries();
  if (matches.length) {
    const recipes = node('fieldset', null, 'recipe-options');
    recipes.setAttribute('role', 'radiogroup'); recipes.setAttribute('aria-label', 'Recipes');
    recipes.append(node('legend', 'Recipes'));
    for (const entry of matches) {
      const selected = entry.id === state.entryId;
      const label = node('label', null, `recipe-option${selected ? ' recipe-option--selected' : ''}`);
      const radio = node('input');
      radio.type = 'radio'; radio.name = 'catalog-recipe'; radio.value = entry.id; radio.checked = selected;
      radio.dataset.focusKey = `recipe:${entry.id}`;
      radio.setAttribute('aria-label', recipeLabel(entry));
      radio.addEventListener('change', () => { state.entryId = radio.value; updateUrl(); render(); });
      const badges = node('span', null, 'recipe-option__badges');
      const scope = node('span', { 'single-node': 'Single-node', 'multi-node': 'Multi-node' }[entry.scope] || entry.scope, 'status-badge status-badge--scope');
      badges.append(scope, maturityBadge(entry.maturity));
      if (entry.blocked) badges.append(node('span', 'Blocked', 'status-badge status-badge--pending'));
      label.append(radio, node('span', workloadLabel(entry.workload_profile), 'recipe-option__name'),
        node('span', `${entry.gpu_allocation.declared_gpus_per_replica ?? 'Unknown'} ${entry.hardware.data.accelerators.model} GPUs per replica (declared)`, 'recipe-option__allocation'),
        node('span', platformKey(entry), 'recipe-option__platform'), badges);
      recipes.append(label);
    }
    result.append(recipes);
  }
  return result;
}
function renderConfigure(entry) {
  const panel = node('div', null, 'configuration-layout');
  const manifests = node('section', null, 'configuration-pane configuration-manifests');
  manifests.setAttribute('aria-label', 'Deployment manifests');
  manifests.append(node('h2', 'Manifests'));
  for (const artifact of entry.artifacts) {
    manifests.append(node('h3', artifact.kinds.join(', ')), sourceLink(artifact.source, 'Artifact source'), drawer(artifact.name, artifact.body));
  }
  const details = node('section', null, 'configuration-pane configuration-details');
  details.setAttribute('aria-label', 'Configuration details');
  details.append(node('h2', 'Configuration details'));
  const inventory = entry.hardware.data.accelerators.count_per_node;
  const summary = table(['Property', 'Value'], [
    ['Checkpoint', entry.serving.model], ['Precision (declared)', entry.quantization || 'Not specified'],
    ['Hardware', `${inventory} ${entry.hardware.data.accelerators.model} GPUs per node`],
    ['Platform', platformKey(entry)],
    ['Deployment scope', { 'single-node': 'Single node', 'multi-node': 'Multiple nodes' }[entry.scope] || entry.scope],
    ['Deployment type', isDisaggregated(entry) ? 'Separate prefill and decode' : 'Combined prefill and decode'],
    ...parallelismRows(entry.gpu_allocation.parallelism),
    ['Benchmark workload target', workloadLabel(entry.workload_profile)]
  ]);
  summary.classList.add('configuration-summary');
  details.append(summary);
  if (isDisaggregated(entry)) details.append(node('p', 'These parallelism settings describe the recipe’s serving configuration, not the total GPU count across prefill and decode. The manifest defines each role.'));
  for (const [role, args] of Object.entries(entry.roles)) {
    if (args.length) { details.append(node('h3', `${role} arguments`)); details.append(table(['Flag', 'Value', 'Why'], args.map(arg => [arg.flag, arg.value ?? null, arg.why]))); }
  }
  details.append(renderRecipeTechnical(entry));
  panel.append(manifests, details);
  return panel;
}
// ---- Benchmark charts ----

const CHART_COLORS = { primary: '#3987e5', secondary: '#d95926', tertiary: '#199e70' };
const CHART_INSTANCES = [];

function destroyCharts() {
  while (CHART_INSTANCES.length) CHART_INSTANCES.pop().destroy();
}

const PERCENTILE_COLORS = {
  p50: '#3987e5', p90: '#8b5cf6', p95: '#d95926', p99: '#dc2626',
};
const PERCENTILE_DASHES = {
  p50: [], p90: [6, 3], p95: [4, 4], p99: [2, 3],
};

function sweepChart(container, yLabel, xValues, datasets) {
  const canvas = document.createElement('canvas');
  container.append(canvas);
  const chart = new Chart(canvas, {
    type: 'line',
    data: {
      labels: xValues.map(String),
      datasets: datasets.map((ds, i) => ({
        label: ds.label,
        data: ds.data,
        borderColor: ds.color || [CHART_COLORS.primary, CHART_COLORS.secondary, CHART_COLORS.tertiary][i % 3],
        backgroundColor: (ds.color || [CHART_COLORS.primary, CHART_COLORS.secondary, CHART_COLORS.tertiary][i % 3]) + '18',
        borderDash: ds.dash || [],
        borderWidth: 2,
        pointRadius: 4,
        pointHoverRadius: 6,
        tension: 0.25,
        fill: datasets.length === 1 && !ds.hidden,
        hidden: ds.hidden || false,
      })),
    },
    options: {
      responsive: true,
      maintainAspectRatio: false,
      plugins: {
        legend: { display: datasets.length > 1, position: 'bottom', labels: { boxWidth: 12, padding: 10, usePointStyle: true, pointStyle: 'circle', font: { size: 11 } } },
        tooltip: { callbacks: { label: ctx => `${ctx.dataset.label}: ${ctx.raw}${unitSuffix(yLabel)}` } },
      },
      scales: {
        x: { title: { display: true, text: 'Concurrency', font: { size: 11 } }, grid: { display: false } },
        y: { title: { display: true, text: yLabel, font: { size: 11 } }, beginAtZero: true, grid: { color: 'rgba(0,0,0,0.06)' } },
      },
    },
  });
  CHART_INSTANCES.push(chart);
}

function unitSuffix(label) {
  if (label.includes('ms')) return ' ms';
  if (label.includes('tok/s')) return ' tok/s';
  if (label.includes('%')) return '%';
  return '';
}

function latencyDatasets(sweep, prefix, defaultOn) {
  const pctls = [
    { key: `${prefix}_p50`, label: 'p50', pctl: 'p50' },
    { key: `${prefix}_p90`, label: 'p90', pctl: 'p90' },
    { key: `${prefix}_p95`, label: 'p95', pctl: 'p95' },
    { key: `${prefix}_p99`, label: 'p99', pctl: 'p99' },
  ];
  return pctls
    .filter(p => sweep.some(pt => pt[p.key] != null))
    .map(p => ({
      label: p.label,
      data: sweep.map(pt => pt[p.key] ?? null),
      color: PERCENTILE_COLORS[p.pctl],
      dash: PERCENTILE_DASHES[p.pctl],
      hidden: !defaultOn.includes(p.pctl),
    }));
}

function statCard(value, label, sub) {
  const card = node('div', null, 'stat-card');
  card.append(node('div', value, 'stat-card__value'), node('div', label, 'stat-card__label'));
  if (sub) card.append(node('div', sub, 'stat-card__sub'));
  return card;
}

function renderSweepCharts(sweep, completeness) {
  const fragment = document.createDocumentFragment();
  const concurrencies = sweep.map(p => p.concurrency);

  // Summary stats from highest concurrency
  const peak = sweep[sweep.length - 1];
  const statsRow = node('div', null, 'stat-row');
  if (peak.output_tok_s != null) statsRow.append(statCard(`${Math.round(peak.output_tok_s)}`, 'Output tok/s', `at C=${peak.concurrency}`));
  if (peak.ttft_ms_p50 != null) statsRow.append(statCard(`${Math.round(peak.ttft_ms_p50)}ms`, 'TTFT p50', `at C=${peak.concurrency}`));
  if (peak.ttft_ms_p95 != null) statsRow.append(statCard(`${Math.round(peak.ttft_ms_p95)}ms`, 'TTFT p95', `at C=${peak.concurrency}`));
  if (peak.itl_ms_p50 != null) statsRow.append(statCard(`${Math.round(peak.itl_ms_p50 * 10) / 10}ms`, 'ITL p50', `at C=${peak.concurrency}`));
  if (peak.itl_ms_p95 != null) statsRow.append(statCard(`${Math.round(peak.itl_ms_p95 * 10) / 10}ms`, 'ITL p95', `at C=${peak.concurrency}`));
  if (peak.context_overflow_rate != null) statsRow.append(statCard(`${(peak.context_overflow_rate * 100).toFixed(1)}%`, 'Context overflow', `at C=${peak.concurrency}`));
  if (peak.ttft_ms_avg != null) statsRow.append(statCard(`${Math.round(peak.ttft_ms_avg)}ms`, 'TTFT avg', `at C=${peak.concurrency}`));
  if (peak.itl_ms_avg != null) statsRow.append(statCard(`${Math.round(peak.itl_ms_avg * 10) / 10}ms`, 'ITL avg', `at C=${peak.concurrency}`));
  if (statsRow.children.length) fragment.append(statsRow);
  if (sweep.length <= 2 || completeness === 'smoke') return fragment;
  if (typeof Chart === 'undefined') {
    fragment.append(node('p', 'Charts unavailable. Summary statistics and run evidence remain available.', 'banner'));
    return fragment;
  }

  // Charts grid
  const grid = node('div', null, 'chart-grid');
  const defaultOn = ['p50', 'p95'];

  // TTFT chart — percentile series, p50 and p95 on by default
  const ttftSets = latencyDatasets(sweep, 'ttft_ms', defaultOn);
  if (ttftSets.length) {
    const card = node('div', null, 'chart-card');
    card.append(node('h3', 'Time to First Token (TTFT)'));
    const container = node('div', null, 'chart-container');
    sweepChart(container, 'TTFT (ms)', sweep.map(p => p.concurrency), ttftSets);
    card.append(container); grid.append(card);
  }

  // ITL chart — percentile series, p50 and p95 on by default
  const itlSets = latencyDatasets(sweep, 'itl_ms', defaultOn);
  if (itlSets.length) {
    const card = node('div', null, 'chart-card');
    card.append(node('h3', 'Inter-Token Latency (ITL)'));
    const container = node('div', null, 'chart-container');
    sweepChart(container, 'ITL (ms)', sweep.map(p => p.concurrency), itlSets);
    card.append(container); grid.append(card);
  }

  // Throughput chart — average, single series
  const tputData = sweep.filter(p => p.output_tok_s != null);
  if (tputData.length > 1) {
    const card = node('div', null, 'chart-card');
    card.append(node('h3', 'Output Throughput'));
    const container = node('div', null, 'chart-container');
    sweepChart(container, 'Output tok/s', tputData.map(p => p.concurrency), [{ label: 'avg tok/s', data: tputData.map(p => p.output_tok_s) }]);
    card.append(container); grid.append(card);
  }

  // Context overflow / failed request rate (AIPerf)
  const rateData = sweep.filter(p => p.context_overflow_rate != null || p.failed_request_rate != null);
  if (rateData.length > 1) {
    const card = node('div', null, 'chart-card');
    card.append(node('h3', 'Error Rates'));
    const container = node('div', null, 'chart-container');
    const datasets = [];
    if (rateData.some(p => p.context_overflow_rate != null)) datasets.push({ label: 'Context overflow (%)', data: rateData.map(p => p.context_overflow_rate != null ? +(p.context_overflow_rate * 100).toFixed(2) : null) });
    if (rateData.some(p => p.failed_request_rate != null)) datasets.push({ label: 'Failed requests (%)', data: rateData.map(p => p.failed_request_rate != null ? +(p.failed_request_rate * 100).toFixed(2) : null) });
    sweepChart(container, 'Rate (%)', rateData.map(p => p.concurrency), datasets);
    card.append(container); grid.append(card);
  }

  if (grid.children.length) fragment.append(grid);
  return fragment;
}

const HARNESS_META = {
  'guidellm': { name: 'GuideLLM', description: 'See each run for workload and completeness' },
  'aiperf': { name: 'AIPerf', description: 'See each run for workload and completeness' },
};

function harnessKey(item) {
  const raw = item.run.harness || item.result.harness || '';
  if (/guidellm/i.test(raw)) return 'guidellm';
  if (/aiperf/i.test(raw)) return 'aiperf';
  return raw || 'unknown';
}

function harnessHeader(key, raw) {
  const meta = HARNESS_META[key];
  const section = node('div', null, 'harness-section');
  const header = node('div', null, 'harness-header');
  const title = node('h2', meta ? meta.name : raw || 'Benchmark');
  header.append(title);
  if (meta) header.append(node('span', meta.description, 'harness-description'));
  else if (raw) {
    const code = node('code', raw);
    const label = node('span', null, 'harness-description');
    label.append(code);
    header.append(label);
  }
  section.append(header);
  return section;
}

function renderEvidenceDetail(item, entry) {
  const detail = document.createDocumentFragment();
  const environment = item.run.environment || {};
  const details = node('details', null, 'evidence-detail');
  details.append(node('summary', `Run: ${item.run.run_id}`));
  details.append(sourceLink(item.source, 'Run source'), node('span', ' '), sourceLink(item.result_source, 'Result source'));
  const forge = item.run.forge;
  if (forge) {
    const link = (url, label) => {
      const anchor = sourceLink({ url }, label);
      if (anchor.tagName !== 'A') return;
      anchor.target = '_blank';
      details.append(node('span', ' · '), anchor);
    };
    link(forge.mlflow_run_url, 'View run in MLflow');
    link(`${forge.mlflow_tracking_uri}/#/experiments/${encodeURIComponent(forge.mlflow_experiment_id)}/runs/${encodeURIComponent(forge.mlflow_parent_run_id)}?workspace=${encodeURIComponent(forge.mlflow_workspace)}`, 'View Forge execution');
    if (forge.prow_job_url) link(forge.prow_job_url, 'Prow job');
    details.append(table(['Forge provenance', 'Value'], [
      ['Tested revision', forge.tested_revision], ['Completeness', forge.completeness],
      ['Model revision', environment.model_revision], ['Image digest', environment.image_digest],
      ['Cluster alias', [...new Set((item.run.workload?.tests || []).map(test => test.cluster).filter(Boolean))].join(', ') || null],
      ['Prow job', forge.prow_job_id], ['PR', forge.pr_number]
    ]));
  }
  details.append(table(['Original run attribution', 'Value'], [
    ['Platform', environment.platform ? `${environment.platform.stack} ${environment.platform.version}` : null],
    ['Engine', environment.vllm_version], ['Image', environment.image],
    ['Hardware profile', item.run.hardware_profile], ['Profile revision when tested', item.run.hardware_profile_revision],
    ['Scope', item.run.deployment_scope]
  ]));
  if (entry.validation.run_id !== item.run.run_id) details.append(node('p', 'Historical evidence for this recipe; equivalence to the selected serving configuration has not been established.'));
  details.append(table(['Metric', 'Value / explicit units and statistics'], Object.entries(item.result.metrics)));
  if (item.run.command) details.append(drawer('benchmark-command.sh', item.run.command));
  details.append(drawer('run.yaml', item.run_body), drawer('result.json', item.result_body));
  detail.append(details);
  return detail;
}

function renderBenchmark(entry) {
  destroyCharts();
  const panel = node('div');
  if (!entry.evidence.length) { panel.append(node('div', 'No committed benchmark evidence for this recipe', 'banner')); return panel; }

  // Group evidence by harness
  const groups = new Map();
  for (const item of entry.evidence) {
    const key = harnessKey(item);
    if (!groups.has(key)) groups.set(key, []);
    groups.get(key).push(item);
  }

  for (const [key, items] of groups) {
    const raw = items[0].run.harness || items[0].result.harness || '';
    const section = harnessHeader(key, raw);

    // Distinct runs/workloads are not points on one performance curve.
    for (const item of items) {
      const sweep = item.result.sweep;
      if (Array.isArray(sweep) && sweep.length > 0) {
        const completeness = item.run.forge?.completeness || item.result.completeness;
        section.append(node('h3', `${item.run.run_id}${completeness ? ` · ${completeness}` : ''}`));
        section.append(renderSweepCharts([...sweep].sort((a, b) => a.concurrency - b.concurrency), completeness));
      }
    }

    // Collapsible run details for each evidence item
    for (const item of items) {
      section.append(renderEvidenceDetail(item, entry));
    }

    panel.append(section);
  }
  return panel;
}
function renderNotes(entry, quickstart = false) {
  const panel = node('div');
  if (quickstart && !entry.notes?.quickstart?.length) {
    panel.append(node('p', 'No deployment walkthrough has been supplied for this recipe.'),
      node('p', 'Open Configuration to review and download the manifests. Cluster setup and verification instructions still need to be documented.'));
    return panel;
  }
  if (!entry.notes) return node('p', 'No optional notes supplied.');
  panel.append(sourceLink(entry.notes_source, 'Notes source'));
  if (quickstart) {
    panel.append(node('p', 'Follow the steps in order. Commands are for you to review and run; this page does not execute them.'));
    for (const step of entry.notes.quickstart || []) {
      const section = node('section', null, 'quickstart-step');
      section.append(node('h2', step.step));
      if (step.detail) section.append(node('p', step.detail));
      if (step.commands) section.append(drawer('Commands', step.commands, false));
      if (step.expected) section.append(node('p', `Expected result: ${step.expected}`, 'quickstart-expected'));
      panel.append(section);
    }
  } else {
    panel.append(node('p', 'Contributor notes are source prose, not measured benchmark evidence; final deployment artifacts remain authoritative.'));
    panel.append(drawer('recipe-notes.json', JSON.stringify(entry.notes, null, 2)));
  }
  return panel;
}
function renderTabs(entry) {
  const tabs = [['start', 'Quick start'], ['config', 'Configuration'], ['bench', 'Benchmark']];
  if (entry.notes) tabs.push(['notes', 'Notes']);
  if (!tabs.some(([id]) => id === state.tab)) state.tab = 'config';
  const result = node('div', null, 'tabs');
  for (const [id, label] of tabs) {
    const button = node('button', label, `tab${state.tab === id ? ' tab--active' : ''}`);
    button.dataset.focusKey = `tab:${id}`;
    button.addEventListener('click', () => { state.tab = id; render(); }); result.append(button);
  }
  return result;
}
function render() {
  const focusKey = app.contains(document.activeElement) ? document.activeElement.dataset.focusKey : null;
  function restoreFocus() {
    if (!focusKey) return;
    const controls = [...app.querySelectorAll('[data-focus-key]')];
    // A removed tab returns to Configure; a removed control returns to the selected recipe
    // or the selected model radio. Do not steal focus on initial load or from outside the app.
    const target = controls.find(control => control.dataset.focusKey === focusKey)
      || (focusKey.startsWith('tab:') && controls.find(control => control.dataset.focusKey === 'tab:config'))
      || controls.find(control => control.dataset.focusKey === `recipe:${state.entryId}`)
      || controls.find(control => control.dataset.focusKey === `model:${state.modelId}`);
    if (target) target.focus({ preventScroll: true });
    else { app.tabIndex = -1; app.focus({ preventScroll: true }); }
  }
  app.replaceChildren();
  if (!CATALOG.models.length) { app.append(node('div', 'No models are available.', 'banner')); restoreFocus(); return; }
  const model = currentModel(), entry = currentView();
  const layout = node('div', null, 'catalog-layout');
  layout.append(node('div', `AI Hub / Models / Catalog / ${model.name || model.id}`, 'crumb'), renderModelCard(model, entry), renderSelectors());
  if (!entry) layout.append(node('div', 'No recipes match this model and selection.', 'banner'));
  else if (entry.blocked) layout.append(node('div', entry.reason || 'This platform entry is blocked.', 'banner--pending'));
  else {
    layout.append(renderTabs(entry));
    const panel = node('div', null, 'panel');
    panel.append(state.tab === 'bench' ? renderBenchmark(entry) : state.tab === 'notes' ? renderNotes(entry) : state.tab === 'start' ? renderNotes(entry, true) : renderConfigure(entry));
    layout.append(panel);
  }
  layout.append(node('p', `Build: ${CATALOG.build.source_sha || 'local'}${CATALOG.build.dirty ? ' — uncommitted local preview' : ''}`));
  app.append(layout);
  restoreFocus();
}
async function boot() {
  try {
    const response = await fetch('./catalog.json');
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    CATALOG = await response.json();
    if (CATALOG.schema_version !== 2 || !Array.isArray(CATALOG.models) || !Array.isArray(CATALOG.entries)) throw new Error('Unsupported catalog contract');
    const query = new URLSearchParams(location.search);
    chooseModel(query.get('model'), query.get('entry')); render();
    window.addEventListener('popstate', () => { const params = new URLSearchParams(location.search); chooseModel(params.get('model'), params.get('entry')); render(); });
  } catch (error) { app.replaceChildren(node('div', `Failed to load catalog: ${error.message}. Preview the generated site over HTTP, not file://.`, 'banner')); }
}
document.addEventListener('DOMContentLoaded', boot);
