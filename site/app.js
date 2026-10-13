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
function renderRecipeTechnical(entry) {
  const technical = node('details', null, 'recipe-technical');
  technical.append(node('summary', 'Technical recipe identity and source rationale'), node('p', `Recipe ID: ${entry.recipe_id}`), node('p', `Platform entry: ${entry.id} — ${platformKey(entry)}`), sourceLink(entry.source, 'Recipe source'));
  const profile = entry.notes?.profile || {};
  const rationale = [profile.label, profile.headline, entry.notes?.default_stance].filter(Boolean);
  if (rationale.length) {
    technical.append(node('p', 'Selection rationale — source prose, not a measured recommendation.'), sourceLink(entry.notes_source, 'Selection rationale source'));
    rationale.forEach(text => technical.append(node('p', text)));
  } else technical.append(node('p', 'No selection rationale supplied'));
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
  const content = node('div'); content.append(node('h1', model.name || model.id));
  const meta = node('div', null, 'model-meta');
  meta.append(node('span', `Provider: ${presentation.provider || model.metadata.huggingface_id?.split('/')[0] || 'Unknown'}`));
  if (entry) {
    meta.append(node('span', `Recipe maturity: ${entry.maturity}`));
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
      label.append(radio, node('span', workloadLabel(entry.workload_profile), 'recipe-option__name'),
        node('span', `${entry.gpu_allocation.declared_gpus_per_replica ?? 'Unknown'} ${entry.hardware.data.accelerators.model} GPUs per replica (declared)`, 'recipe-option__allocation'),
        node('span', `${entry.scope} — ${platformKey(entry)}`, 'recipe-option__platform'),
        node('span', `${entry.maturity}${entry.blocked ? ' — blocked' : ''}`, 'recipe-option__maturity'));
      recipes.append(label);
    }
    result.append(recipes);
  }
  return result;
}
function renderConfigure(entry) {
  const panel = node('div');
  panel.append(renderRecipeTechnical(entry));
  const inventory = entry.hardware.data.accelerators.count_per_node;
  panel.append(table(['Property', 'Value'], [
    ['Platform', platformKey(entry)], ['Hardware profile', `${entry.hardware.path} revision ${entry.hardware.revision}`],
    ['Host inventory (per node)', `${inventory} accelerators`], ['Declared serving allocation (per replica)', `${entry.gpu_allocation.declared_gpus_per_replica} GPUs`],
    ['Scope', entry.scope], ['Workload', workloadLabel(entry.workload_profile)], ['Optimization intent (declared, not a measurement)', entry.optimization_intent], ['Parallelism', entry.gpu_allocation.parallelism], ['Image', entry.serving.image],
    ['Image kind', entry.serving.image_usage?.kind || 'Unclassified legacy image'], ['Custom image note', entry.serving.image_usage?.note || null]
  ]));
  panel.append(sourceLink(entry.source, 'Recipe source'), node('p', entry.command_note));
  for (const [role, args] of Object.entries(entry.roles)) {
    if (args.length) { panel.append(node('h2', `${role} arguments`)); panel.append(table(['Flag', 'Value', 'Why'], args.map(arg => [arg.flag, arg.value ?? null, arg.why]))); }
  }
  for (const spec of entry.specs) panel.append(node('p', `${spec.label}: ${spec.state === 'resolved' ? JSON.stringify(spec.value) : 'Unverified source note — inspect final artifacts'} (${spec.source})`));
  for (const artifact of entry.artifacts) {
    panel.append(node('h2', artifact.kinds.join(', ')), sourceLink(artifact.source, 'Artifact source'), drawer(artifact.name, artifact.body));
  }
  return panel;
}
function renderBenchmark(entry) {
  const panel = node('div');
  if (!entry.evidence.length) { panel.append(node('div', 'No committed benchmark evidence for this recipe', 'banner')); return panel; }
  for (const item of entry.evidence) {
    panel.append(node('h2', `Original run: ${item.run.run_id}`), sourceLink(item.source, 'Run source'), sourceLink(item.result_source, 'Result source'));
    const environment = item.run.environment || {};
    panel.append(table(['Original run attribution', 'Value'], [
      ['Platform', environment.platform ? `${environment.platform.stack} ${environment.platform.version}` : null],
      ['Engine', environment.vllm_version], ['Image', environment.image],
      ['Hardware profile', item.run.hardware_profile], ['Profile revision when tested', item.run.hardware_profile_revision],
      ['Scope', item.run.deployment_scope]
    ]));
    if (entry.validation.run_id !== item.run.run_id) panel.append(node('p', 'Historical evidence for this recipe; equivalence to the selected serving configuration has not been established.'));
    panel.append(table(['Metric', 'Value / explicit units and statistics'], Object.entries(item.result.metrics)));
    if (item.run.command) panel.append(drawer('benchmark-command.sh', item.run.command));
    panel.append(drawer('run.yaml', item.run_body), drawer('result.json', item.result_body));
  }
  return panel;
}
function renderNotes(entry, quickstart = false) {
  const panel = node('div');
  if (quickstart && !entry.notes?.quickstart?.length) {
    panel.append(node('p', 'Quickstart notes have not been supplied for this recipe.'),
      node('p', 'Examples for future prerequisite notes, not requirements for this recipe: PVC access mode (RWO/RWX), weights pre-download, secrets/access, required operators/networking.'));
    return panel;
  }
  if (!entry.notes) return node('p', 'No optional notes supplied.');
  panel.append(sourceLink(entry.notes_source, 'Notes source'));
  panel.append(node('p', 'Contributor notes are source prose, not measured benchmark evidence; final deployment artifacts remain authoritative.'));
  if (quickstart) {
    for (const step of entry.notes.quickstart || []) panel.append(node('h2', step.step), node('p', step.detail));
  } else {
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
