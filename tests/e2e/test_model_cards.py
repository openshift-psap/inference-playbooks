"""Visible native model radios preserve catalog selection and keyboard behavior."""
import copy
from urllib.parse import parse_qs, urlparse

import pytest
from playwright.sync_api import expect


def open_models(page, served, query='', hostile=False):
    payload = copy.deepcopy(served[1])
    template = copy.deepcopy(payload['models'][0])
    payload['models'] = []
    payload['entries'] = []
    for index in range(3):
        model = copy.deepcopy(template)
        model.update(id=f'model-{index}', name=f'Model {index}')
        if hostile and index == 1:
            model.update(id='<img src=x onerror="window.injected=true">',
                         name='<script>window.injected=true</script>')
        payload['models'].append(model)
        for variant in range(2):
            entry = copy.deepcopy(served[1]['entries'][0])
            entry.update(id=f'entry-{index}-{variant}', model_id=model['id'],
                         blocked=False, scope=['single-node', 'multi-node'][variant],
                         workload_profile=['guidellm-8k1k', 'aiperf-agentx-128k'][variant])
            entry['platform'] = {'stack': 'vllm', 'version': str(variant + 1)}
            entry['hardware']['accelerator_key'] = f'accelerator-{variant}'
            payload['entries'].append(entry)
    page.route('**/catalog.json', lambda route: route.fulfill(json=payload))
    page.goto(served[0] + query)
    expect(page.get_by_role('radiogroup', name='Models', exact=True)).to_be_visible()
    return payload


def test_all_models_visible_in_catalog_order(page, served):
    payload = open_models(page, served)
    cards = page.locator('.model-option')
    expect(cards).to_have_count(len(payload['models']))
    expect(page.locator('select[aria-label="Model"]')).to_have_count(0)
    expect(page.locator('.model-options legend')).to_have_text('Models')
    assert cards.locator('.model-option__id').all_text_contents() == [m['id'] for m in payload['models']]
    for index, model in enumerate(payload['models']):
        expect(cards.nth(index)).to_be_visible()
        expect(cards.nth(index).locator('.model-option__name')).to_have_text(model['name'])
        expect(cards.nth(index).locator('input')).to_have_attribute('data-focus-key', f"model:{model['id']}")
    expect(page.locator('input[name="catalog-model"]:checked')).to_have_count(1)
    expect(page.locator('.model-option--selected')).to_have_count(1)
    expect(page.get_by_role('radio', name='Model 0', exact=True)).to_be_checked()


def test_model_switch_resets_filters_and_updates_detail_url(page, served):
    open_models(page, served)
    page.get_by_role('radiogroup', name='Platform', exact=True).get_by_role('radio', name='vLLM', exact=True).check()
    page.get_by_role('radiogroup', name='Version', exact=True).get_by_role('radio', name='2', exact=True).check()
    for group, option in [('GPU model', 'H200'), ('Scope', 'Multi-node'), ('Workload', 'Agentic workload')]:
        page.get_by_role('radiogroup', name=group, exact=True).get_by_role('radio', name=option, exact=True).check()
    page.get_by_role('button', name='Benchmark', exact=True).click()
    page.locator('.model-option').nth(1).click()
    expect(page.get_by_role('radio', name='Model 1', exact=True)).to_be_checked()
    expect(page.locator('input[name="catalog-model"]:checked')).to_have_count(1)
    expect(page.locator('.model-card h1')).to_have_text('Model 1')
    expect(page.locator('.model-option--selected .model-option__id')).to_have_text('model-1')
    expect(page.get_by_role('radiogroup', name='Platform', exact=True).get_by_role('radio', name='All', exact=True)).to_be_checked()
    expect(page.get_by_role('radiogroup', name='Version', exact=True).locator('input:checked')).to_have_value('')
    expect(page.get_by_role('radiogroup', name='Version', exact=True).get_by_role('radio', name='All versions', exact=True)).to_be_disabled()
    for group in ['GPU model', 'Scope', 'Workload']:
        expect(page.get_by_role('radiogroup', name=group, exact=True).get_by_role('radio', name='All', exact=True)).to_be_checked()
    expect(page.locator('input[name="catalog-recipe"]:checked')).to_have_value('entry-1-0')
    expect(page.get_by_role('button', name='Configuration', exact=True)).to_have_class('tab tab--active')
    assert parse_qs(urlparse(page.url).query) == {'model': ['model-1'], 'entry': ['entry-1-0']}


@pytest.mark.parametrize('query,model,entry', [
    ('?model=model-2&entry=entry-2-1', 'Model 2', 'entry-2-1'),
    ('?model=missing&entry=missing', 'Model 0', 'entry-0-0'),
    ('?model=model-1&entry=entry-2-1', 'Model 1', 'entry-1-0'),
])
def test_deep_link_and_fallback(page, served, query, model, entry):
    open_models(page, served, query)
    expect(page.get_by_role('radio', name=model, exact=True)).to_be_checked()
    expect(page.locator('input[name="catalog-recipe"]:checked')).to_have_value(entry)
    expect(page.locator('input[name="catalog-model"]:checked')).to_have_count(1)


def test_native_keyboard_navigation_restores_focus(page, served):
    open_models(page, served)
    page.get_by_role('radio', name='Model 0', exact=True).focus()
    for key, name in [('ArrowRight', 'Model 1'), ('ArrowDown', 'Model 2'),
                      ('ArrowRight', 'Model 0'), ('ArrowLeft', 'Model 2')]:
        page.keyboard.press(key)
        expect(page.get_by_role('radio', name=name, exact=True)).to_be_checked()
        expect(page.get_by_role('radio', name=name, exact=True)).to_be_focused()
        expect(page.locator('input[name="catalog-model"]:checked')).to_have_count(1)
    page.keyboard.press('Tab')
    expect(page.get_by_role('radiogroup', name='Platform', exact=True).get_by_role('radio', name='All', exact=True)).to_be_focused()


def test_removed_selector_focus_falls_back_to_selected_radio(page, served):
    open_models(page, served)
    page.locator('input[name="catalog-recipe"]:checked').focus()
    page.evaluate("""() => {
        CATALOG.entries = [];
        history.pushState(null, '', '?model=model-1');
        dispatchEvent(new PopStateEvent('popstate'));
    }""")
    expect(page.get_by_role('radio', name='Model 1', exact=True)).to_be_focused()
    expect(page.get_by_role('radio', name='Model 1', exact=True)).to_be_checked()


def test_hostile_model_text_is_literal(page, served):
    payload = open_models(page, served, hostile=True)
    model = payload['models'][1]
    card = page.locator('.model-option').nth(1)
    expect(card.locator('.model-option__name')).to_have_text(model['name'])
    expect(card.locator('.model-option__id')).to_have_text(model['id'])
    card.click()
    expect(page.get_by_role('radio', name=model['name'], exact=True)).to_be_checked()
    expect(page.locator('.model-card h1')).to_have_text(model['name'])
    expect(page.locator('#app script, #app img')).to_have_count(0)
    assert page.evaluate('window.injected === undefined')


def test_model_without_name_uses_id(page, served):
    open_models(page, served)
    page.evaluate("""() => {
        delete CATALOG.models[1].name;
        chooseModel('model-1');
        render();
    }""")
    expect(page.get_by_role('radio', name='model-1', exact=True)).to_be_checked()
    expect(page.locator('.model-option--selected .model-option__name')).to_have_text('model-1')
    expect(page.locator('.model-card h1')).to_have_text('model-1')
