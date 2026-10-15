"""Keyboard focus survives replacement of catalog controls."""
import copy

import pytest
from playwright.sync_api import expect


def focus_catalog(page, served):
    payload = copy.deepcopy(served[1])
    model = copy.deepcopy(payload['models'][0])
    other_model = copy.deepcopy(model)
    other_model.update(id='focus-other', name='Other focus model')
    first = copy.deepcopy(payload['entries'][0])
    first.update(id='focus-first', model_id=model['id'], blocked=False,
                  scope='single-node', workload_profile='guidellm-8k1k')
    first['platform'] = {'stack': 'vllm', 'version': '1'}
    first['hardware']['accelerator_key'] = 'a-accelerator'
    first['hardware']['data']['accelerators']['model'] = 'B300'
    first['notes'] = {'quickstart': [{'step': 'Start', 'detail': 'Example'}]}
    second = copy.deepcopy(first)
    second.update(id='focus-second', recipe_id='other-recipe', scope='multi-node',
                   workload_profile='aiperf-agentx-128k')
    second['platform']['version'] = '2'
    second['hardware']['accelerator_key'] = 'b-accelerator'
    second['hardware']['data']['accelerators']['model'] = 'B200'
    other = copy.deepcopy(first)
    other.update(id='focus-other-entry', model_id=other_model['id'])
    payload['models'] = [model, other_model]
    payload['entries'] = [first, second, other]
    page.route('**/catalog.json', lambda route: route.fulfill(json=payload))
    page.goto(served[0])
    expect(page.locator('input[name="catalog-model"]:checked')).to_be_visible()
    return payload


def test_model_keyboard_focus_and_next_tab(page, served):
    payload = focus_catalog(page, served)
    model = page.get_by_role('radio', name=payload['models'][0]['name'], exact=True)
    model.focus()
    model.press('ArrowRight')
    expect(page.get_by_role('radio', name='Other focus model', exact=True)).to_be_checked()
    expect(page.get_by_role('radio', name='Other focus model', exact=True)).to_be_focused()
    page.keyboard.press('Tab')
    expect(page.get_by_role('radiogroup', name='Platform', exact=True).get_by_role('radio', name='All', exact=True)).to_be_focused()


@pytest.mark.parametrize('label,next_label', [
    ('Platform', 'Version'),
    ('Version', 'GPU model'),
    ('GPU model', 'Scope'),
    ('Scope', 'Workload'),
    ('Workload', 'Recipe'),
])
def test_filter_keyboard_focus_and_next_tab(page, served, label, next_label):
    focus_catalog(page, served)
    if label == 'Version':
        page.get_by_role('radiogroup', name='Platform', exact=True).get_by_role('radio', name='vLLM', exact=True).check()
    group = page.get_by_role('radiogroup', name=label, exact=True)
    group.locator('input:checked').focus()
    page.keyboard.press('ArrowRight')
    expect(group.locator('input:checked')).to_be_focused()
    assert group.locator('input:checked').input_value()
    page.keyboard.press('Tab')
    if next_label == 'Recipe':
        expect(page.locator('input[name="catalog-recipe"]:checked')).to_be_focused()
    else:
        expect(page.get_by_role('radiogroup', name=next_label, exact=True).locator('input:checked')).to_be_focused()


def test_recipe_keyboard_focus_and_next_tab(page, served):
    focus_catalog(page, served)
    recipe = page.locator('input[name="catalog-recipe"]:checked')
    recipe.focus()
    recipe.press('ArrowRight')
    expect(page.locator('input[name="catalog-recipe"]:checked')).to_have_value('focus-second')
    expect(page.locator('input[name="catalog-recipe"]:checked')).to_be_focused()
    page.keyboard.press('Tab')
    expect(page.get_by_role('button', name='Quick start', exact=True)).to_be_focused()


@pytest.mark.parametrize('name,next_name', [
    ('Quick start', 'Configuration'), ('Configuration', 'Benchmark'), ('Benchmark', 'Notes'),
])
def test_tab_keyboard_focus_and_next_tab(page, served, name, next_name):
    focus_catalog(page, served)
    page.get_by_role('button', name=name, exact=True).focus()
    page.keyboard.press('Enter')
    expect(page.get_by_role('button', name=name, exact=True)).to_be_focused()
    expect(page.get_by_role('button', name=name, exact=True)).to_have_class('tab tab--active')
    page.keyboard.press('Tab')
    expect(page.get_by_role('button', name=next_name, exact=True)).to_be_focused()


def test_persistent_scope_focus_survives_model_change(page, served):
    focus_catalog(page, served)
    scope = page.get_by_role('radiogroup', name='Scope', exact=True)
    scope.get_by_role('radio', name='Single-node', exact=True).check()
    page.evaluate("""() => {
        history.pushState(null, '', '?model=focus-other');
        dispatchEvent(new PopStateEvent('popstate'));
    }""")
    expect(scope.get_by_role('radio', name='Single-node', exact=True)).to_be_focused()
    expect(scope.get_by_role('radio', name='All', exact=True)).to_be_checked()
    page.keyboard.press('Tab')
    expect(page.get_by_role('radiogroup', name='Workload', exact=True).locator('input:checked')).to_be_focused()


def test_notes_keyboard_focus_and_next_tab(page, served):
    focus_catalog(page, served)
    page.get_by_role('button', name='Notes', exact=True).focus()
    page.keyboard.press('Enter')
    expect(page.get_by_role('button', name='Notes', exact=True)).to_be_focused()
    page.keyboard.press('Tab')
    expect(page.locator('.panel').locator('a, summary, button').first).to_be_focused()


def test_disappearing_tab_focus_falls_back_to_configure(page, served):
    focus_catalog(page, served)
    page.get_by_role('button', name='Notes', exact=True).focus()
    page.evaluate("""() => {
        CATALOG.entries.find(entry => entry.id === 'focus-other-entry').notes = null;
        history.pushState(null, '', '?model=focus-other');
        dispatchEvent(new PopStateEvent('popstate'));
    }""")
    expect(page.get_by_role('button', name='Notes', exact=True)).to_have_count(0)
    expect(page.get_by_role('button', name='Configuration', exact=True)).to_be_focused()
    page.keyboard.press('Tab')
    expect(page.get_by_role('button', name='Benchmark', exact=True)).to_be_focused()
