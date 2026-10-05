"""Platform availability depends on model entries, not other active filters."""
import copy

import pytest
from playwright.sync_api import expect


FAMILIES = ['vLLM', 'RHOAI / RHAII', 'llm-d']


def platform(page, name):
    return page.get_by_role('radiogroup', name='Platform', exact=True).get_by_role('radio', name=name, exact=True)


@pytest.mark.parametrize('model,supported', [
    ('gemma-4', ['vLLM']), ('qwen3-235b-a22b', ['RHOAI / RHAII']),
    ('glm-5.2', ['vLLM', 'RHOAI / RHAII']),
])
def test_actual_model_availability(page, served, model, supported):
    page.goto(f'{served[0]}?model={model}')
    expect(platform(page, 'All')).to_be_enabled()
    expect(platform(page, 'All')).to_be_checked()
    for name in FAMILIES:
        radio = platform(page, name)
        expect(radio.locator('..')).to_be_visible()
        if name in supported:
            expect(radio).to_be_enabled()
        else:
            expect(radio).to_be_disabled()
            expect(radio.locator('..')).to_have_class('filter-option filter-option--unavailable')
            expect(radio.locator('..')).to_have_attribute('title', 'No recipes for this platform family and selected model.')


def test_disabled_label_click_inert_keyboard_skips_and_model_switch_updates(page, served):
    page.goto(f'{served[0]}?model=gemma-4')
    expect(platform(page, 'RHOAI / RHAII')).to_be_disabled()
    before = page.evaluate('JSON.stringify(state)')
    # Attempt the physical click despite Playwright's disabled-label guard.
    platform(page, 'RHOAI / RHAII').locator('..').click(force=True)
    platform(page, 'RHOAI / RHAII').evaluate('(radio) => radio.click()')
    assert page.evaluate('JSON.stringify(state)') == before
    platform(page, 'All').focus()
    page.keyboard.press('ArrowRight')
    expect(platform(page, 'vLLM')).to_be_checked()
    expect(platform(page, 'vLLM')).to_be_focused()
    page.keyboard.press('ArrowRight')
    expect(platform(page, 'All')).to_be_checked()
    expect(platform(page, 'All')).to_be_focused()
    page.locator('input[name="catalog-model"][value="qwen3-235b-a22b"]').check()
    expect(platform(page, 'All')).to_be_checked()
    expect(platform(page, 'vLLM')).to_be_disabled()
    expect(platform(page, 'RHOAI / RHAII')).to_be_enabled()


def test_other_filters_no_results_do_not_disable_supported_platform(page, served):
    page.goto(f'{served[0]}?model=glm-5.2')
    page.get_by_role('radiogroup', name='GPU model').get_by_role('radio', name='B300', exact=True).check()
    expect(page.locator('.recipe-option')).to_have_count(0)
    expect(platform(page, 'vLLM')).to_be_enabled()
    expect(platform(page, 'RHOAI / RHAII')).to_be_enabled()
    platform(page, 'vLLM').check()
    expect(page.locator('.recipe-option')).to_have_count(0)
    expect(page.get_by_role('radiogroup', name='GPU model').get_by_role('radio', name='B300', exact=True)).to_be_checked()
    expect(platform(page, 'vLLM')).to_be_enabled()
    expect(platform(page, 'RHOAI / RHAII')).to_be_enabled()


def test_empty_model_and_blocked_only_family(page, served):
    payload = copy.deepcopy(served[1])
    payload['models'] = payload['models'][:2]
    entry = copy.deepcopy(payload['entries'][0])
    entry.update(id='blocked-only', model_id=payload['models'][1]['id'], blocked=True,
                 reason='Synthetic blocked platform remains inspectable')
    entry['platform'] = {'stack': 'llm-d', 'version': 'example-version'}
    payload['entries'] = [entry]
    page.route('**/catalog.json', lambda route: route.fulfill(json=payload))
    page.goto(served[0])
    expect(platform(page, 'All')).to_be_enabled()
    for name in FAMILIES:
        expect(platform(page, name)).to_be_disabled()
    expect(page.locator('.recipe-option')).to_have_count(0)
    page.locator('input[name="catalog-model"]').nth(1).check()
    expect(platform(page, 'llm-d')).to_be_enabled()
    expect(platform(page, 'vLLM')).to_be_disabled()
    expect(platform(page, 'RHOAI / RHAII')).to_be_disabled()
    platform(page, 'llm-d').check()
    expect(page.locator('.recipe-option')).to_have_count(1)
    expect(page.locator('.banner--pending')).to_contain_text(entry['reason'])
