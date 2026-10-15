"""Compact metadata and honest always-available quickstart notes."""
import copy

import pytest
from playwright.sync_api import expect


@pytest.mark.parametrize('model', ['qwen3-235b-a22b', 'gemma-4', 'glm-5.2'])
def test_compact_header_and_layout_hooks(page, served, model):
    page.goto(f'{served[0]}?model={model}')
    selected = page.locator('input[name="catalog-recipe"]:checked')
    expect(selected).to_have_count(1)
    entry = next(entry for entry in served[1]['entries'] if entry['id'] == selected.input_value())
    meta = page.locator('.model-card .model-meta')
    expect(meta).to_be_visible()
    expect(meta.locator('p')).to_have_count(0)
    expect(meta).to_contain_text('Provider:')
    expect(meta).to_contain_text(f"Recipe maturity: {entry['maturity']}")
    expect(meta).to_contain_text(f"Engine: {entry['engine']['version'] or 'Unknown'} ({entry['engine']['source'] or 'unresolved'})")
    assert page.locator('.catalog-layout').get_attribute('style') is None
    assert page.locator('.filter-pair > fieldset').evaluate_all('(els) => els.map(el => el.dataset.filter)') == ['scope', 'workload']
    expect(page.get_by_role('button', name='Quick start', exact=True)).to_be_visible()


def test_missing_qwen_quickstart_is_neutral_placeholder(page, served):
    page.goto(f'{served[0]}?model=qwen3-235b-a22b')
    page.get_by_role('button', name='Quick start', exact=True).click()
    panel = page.locator('.panel')
    expect(panel).to_contain_text('Quickstart notes have not been supplied for this recipe.')
    expect(panel).to_contain_text('not requirements for this recipe')
    expect(panel).to_contain_text('PVC access mode (RWO/RWX)')
    expect(panel).to_contain_text('weights pre-download')
    assert 'requires RWX' not in panel.inner_text()
    expect(panel.locator('pre')).to_have_count(0)


def test_populated_steps_preserve_source_without_placeholder(page, served):
    entry = next(entry for entry in served[1]['entries'] if not entry['blocked'] and (entry.get('notes') or {}).get('quickstart'))
    page.goto(served[0])
    expect(page.locator('.model-card')).to_be_visible()
    page.evaluate('([model, entry]) => { chooseModel(model, entry); render(); }', [entry['model_id'], entry['id']])
    page.get_by_role('button', name='Quick start', exact=True).click()
    panel = page.locator('.panel')
    expect(panel).not_to_contain_text('Quickstart notes have not been supplied')
    for step in entry['notes']['quickstart']:
        expect(panel).to_contain_text(step['step'])
        expect(panel).to_contain_text(step['detail'])
    if entry['notes_source']['url']:
        expect(panel.get_by_role('link', name='Notes source')).to_have_attribute('href', entry['notes_source']['url'])
    else:
        expect(panel).to_contain_text(entry['notes_source']['path'])


def test_validation_attribution_and_empty_steps_preserved(page, served):
    payload = copy.deepcopy(served[1])
    entry = copy.deepcopy(payload['entries'][0])
    entry.update(model_id=payload['models'][0]['id'], blocked=False, maturity='contributed', notes={'quickstart': []})
    entry['validation'].update(label='Historical source validation', qualification='Selected platform has not been independently validated.',
                               tested_platform={'stack': 'vllm', 'version': 'source-version'})
    entry['engine'] = {'version': None, 'source': 'unresolved source mapping'}
    payload['entries'] = [entry]
    page.route('**/catalog.json', lambda route: route.fulfill(json=payload))
    page.goto(served[0])
    expect(page.locator('.model-meta')).to_contain_text('Recipe maturity: contributed')
    expect(page.locator('.model-meta')).to_contain_text('Historical source validation')
    expect(page.locator('.model-meta')).to_contain_text('Engine: Unknown (unresolved source mapping)')
    expect(page.locator('.model-card')).to_contain_text('Selected platform has not been independently validated. Tested on vllm source-version.')
    page.get_by_role('button', name='Quick start', exact=True).click()
    expect(page.locator('.panel')).to_contain_text('Quickstart notes have not been supplied for this recipe.')
