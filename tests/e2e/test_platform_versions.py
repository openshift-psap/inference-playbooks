"""Platform family presentation and versions use only actual model entries."""
import copy

from playwright.sync_api import expect


def platform(page, name):
    return page.get_by_role('radiogroup', name='Platform', exact=True).get_by_role('radio', name=name, exact=True)


def versions(page):
    return page.get_by_role('radiogroup', name='Version', exact=True)


def synthetic(page, served):
    payload = copy.deepcopy(served[1])
    payload['models'] = payload['models'][:2]
    base = copy.deepcopy(payload['entries'][0])
    payload['entries'] = []
    for index, (stack, version, scope) in enumerate([
        ('vllm', 'v0.24.0', 'single-node'),
        ('vllm', 'v0.23.1', 'multi-node'),
        ('rhoai', '3.5', 'single-node'),
        ('rhaii', '3.6-preview', 'multi-node'),
    ]):
        entry = copy.deepcopy(base)
        entry.update(id=f'platform-{index}', model_id=payload['models'][0]['id'], scope=scope, blocked=False)
        entry['platform'] = {'stack': stack, 'version': version}
        entry['engine'] = {'version': 'v9.99.99', 'source': 'synthetic engine, not platform version'}
        payload['entries'].append(entry)
    other = copy.deepcopy(base)
    other.update(id='other-model-entry', model_id=payload['models'][1]['id'], blocked=False)
    other['platform'] = {'stack': 'vllm', 'version': 'v0.22.0'}
    payload['entries'].append(other)
    page.route('**/catalog.json', lambda route: route.fulfill(json=payload))
    page.goto(served[0])
    expect(platform(page, 'All')).to_be_checked()
    return payload


def option_values(page):
    return versions(page).locator('input[type="radio"]').evaluate_all('(els) => els.map(el => el.value)')


def recipe_values(page):
    return page.locator('input[name="catalog-recipe"]').evaluate_all('(els) => els.map(el => el.value)')


def test_fixed_families_all_union_and_actual_model_versions(page, served):
    payload = synthetic(page, served)
    for name in ['All', 'vLLM', 'RHOAI / RHAII', 'llm-d']:
        expect(platform(page, name)).to_be_visible()
    expect(versions(page).get_by_role('radio', name='All versions', exact=True)).to_be_disabled()
    assert option_values(page) == ['']
    assert recipe_values(page) == ['platform-0', 'platform-1', 'platform-2', 'platform-3']
    platform(page, 'vLLM').check()
    expect(versions(page).get_by_role('radio', name='All versions', exact=True)).to_be_enabled()
    assert option_values(page) == ['', 'v0.23.1', 'v0.24.0']
    assert 'v9.99.99' not in option_values(page)
    page.locator('input[name="catalog-model"]').nth(1).check()
    expect(platform(page, 'All')).to_be_checked()
    expect(versions(page).get_by_role('radio', name='All versions', exact=True)).to_be_disabled()
    platform(page, 'vLLM').check()
    assert option_values(page) == ['', payload['entries'][-1]['platform']['version']]


def test_unavailable_family_honest_and_keyboard_recovery(page, served):
    synthetic(page, served)
    unavailable = platform(page, 'llm-d')
    expect(unavailable).to_be_visible()
    expect(unavailable).to_be_disabled()
    assert unavailable.evaluate('radio => getComputedStyle(radio.closest("label")).cursor') == 'not-allowed'
    # Deliberately click a disabled label to prove the browser leaves state intact.
    unavailable.locator('..').click(force=True)
    expect(platform(page, 'All')).to_be_checked()
    expect(versions(page).get_by_role('radio', name='All versions', exact=True)).to_be_disabled()
    assert option_values(page) == ['']
    assert len(recipe_values(page)) == 4
    platform(page, 'RHOAI / RHAII').focus()
    page.keyboard.press('ArrowRight')
    expect(platform(page, 'All')).to_be_focused()
    expect(page.locator('.platform-unavailable')).to_have_count(0)


def test_family_change_clears_only_version_preserves_strict_criteria(page, served):
    synthetic(page, served)
    platform(page, 'vLLM').check()
    versions(page).get_by_role('radio', name='v0.24.0', exact=True).check()
    page.get_by_role('radiogroup', name='Scope').get_by_role('radio', name='Multi-node').check()
    expect(page.locator('.recipe-option')).to_have_count(0)
    expect(versions(page).locator('input:checked')).to_have_value('v0.24.0')
    platform(page, 'RHOAI / RHAII').check()
    expect(versions(page).locator('input:checked')).to_have_value('')
    assert option_values(page) == ['', '3.5', '3.6-preview']
    expect(page.get_by_role('radiogroup', name='Scope').get_by_role('radio', name='Multi-node')).to_be_checked()
    assert recipe_values(page) == ['platform-3']
    expect(page.locator('.recipe-option')).to_contain_text('rhaii 3.6-preview')
    page.locator('.recipe-technical summary').click()
    expect(page.locator('.recipe-technical')).to_contain_text('rhaii 3.6-preview')
    platform(page, 'All').check()
    expect(versions(page).get_by_role('radio', name='All versions', exact=True)).to_be_disabled()
    assert recipe_values(page) == ['platform-1', 'platform-3']


def test_focus_order_and_native_platform_navigation(page, served):
    synthetic(page, served)
    page.locator('input[name="catalog-model"]:checked').focus()
    page.keyboard.press('Tab')
    expect(platform(page, 'All')).to_be_focused()
    page.keyboard.press('ArrowRight')
    expect(platform(page, 'vLLM')).to_be_checked()
    expect(platform(page, 'vLLM')).to_be_focused()
    page.keyboard.press('Tab')
    expect(versions(page).get_by_role('radio', name='All versions', exact=True)).to_be_focused()
    versions(page).get_by_role('radio', name='v0.23.1', exact=True).check()
    expect(versions(page).get_by_role('radio', name='v0.23.1', exact=True)).to_be_focused()
    expect(versions(page).get_by_role('radio', name='v0.23.1', exact=True)).to_have_attribute('data-focus-key', 'filter:version:v0.23.1')
    page.keyboard.press('Tab')
    expect(page.get_by_role('radiogroup', name='GPU model').get_by_role('radio', name='All', exact=True)).to_be_focused()
