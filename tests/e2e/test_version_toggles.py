"""Exact catalog versions use native pills and numeric display ordering."""
import copy

from playwright.sync_api import expect


def group(page, name):
    return page.get_by_role('radiogroup', name=name, exact=True)


def choose(page, name, value):
    group(page, name).get_by_role('radio', name=value, exact=True).check()


def synthetic(page, served):
    payload = copy.deepcopy(served[1])
    base = copy.deepcopy(payload['entries'][0])
    raw = ['3.10', '0.10', '3.9', '0.9', '3.5.10', '3.5.2',
           '3.5-ea10', '3.5-ea2', '3.5-ea1', '3.5', '3.5.0',
           'v0.24.0', 'V0.23.0', 'Vendor-dev', 'vendor-dev']
    payload['entries'] = []
    for index, version in enumerate(raw):
        entry = copy.deepcopy(base)
        entry.update(id=f'version-{index}', model_id=payload['models'][0]['id'], blocked=False, scope='single-node')
        entry['platform'] = {'stack': 'vllm', 'version': version}
        payload['entries'].append(entry)
    other = copy.deepcopy(base)
    other.update(id='rhoai-entry', model_id=payload['models'][0]['id'], blocked=False, scope='multi-node')
    other['platform'] = {'stack': 'rhoai', 'version': '3.6'}
    payload['entries'].append(other)
    page.route('**/catalog.json', lambda route: route.fulfill(json=payload))
    page.goto(served[0])
    expect(group(page, 'Version')).to_be_visible()
    return payload


def test_numeric_order_raw_identity_and_no_dropdown(page, served):
    synthetic(page, served)
    choose(page, 'Platform', 'vLLM')
    radios = group(page, 'Version').locator('input')
    assert radios.evaluate_all('(els) => els.map(el => el.value)') == [
        '', '0.9', '0.10', 'V0.23.0', 'v0.24.0', '3.5-ea1', '3.5-ea2',
        '3.5-ea10', '3.5', '3.5.0', '3.5.2', '3.5.10', '3.9', '3.10', 'Vendor-dev', 'vendor-dev']
    expect(page.locator('select')).to_have_count(0)
    choose(page, 'Version', '3.5.0')
    expect(group(page, 'Version').locator('input:checked')).to_have_value('3.5.0')
    expect(page.locator('.recipe-option')).to_have_count(1)
    expect(page.locator('.recipe-option')).to_contain_text('vllm 3.5.0')
    expect(group(page, 'Version').get_by_role('radio', name='3.5.0', exact=True)).to_have_attribute('data-focus-key', 'filter:version:3.5.0')
    expect(group(page, 'Version').locator('.filter-option--selected')).to_have_count(1)


def test_all_union_family_reset_preserves_scope(page, served):
    payload = synthetic(page, served)
    expect(group(page, 'Version').get_by_role('radio', name='All versions')).to_be_disabled()
    expect(page.locator('.recipe-option')).to_have_count(len(payload['entries']))
    choose(page, 'Platform', 'vLLM')
    choose(page, 'Version', '0.9')
    choose(page, 'Scope', 'Multi-node')
    expect(page.locator('.recipe-option')).to_have_count(0)
    choose(page, 'Platform', 'RHOAI / RHAII')
    expect(group(page, 'Version').get_by_role('radio', name='All versions')).to_be_checked()
    expect(group(page, 'Scope').get_by_role('radio', name='Multi-node')).to_be_checked()
    expect(page.locator('.recipe-option')).to_have_count(1)
    choose(page, 'Scope', 'All')
    choose(page, 'Platform', 'vLLM')
    choose(page, 'Version', '0.9')
    choose(page, 'Version', 'All versions')
    expect(page.locator('.recipe-option')).to_have_count(len(payload['entries']) - 1)


def test_native_keyboard_focus_and_disabled_group_skip(page, served):
    synthetic(page, served)
    group(page, 'Platform').get_by_role('radio', name='All', exact=True).focus()
    page.keyboard.press('Tab')
    expect(group(page, 'GPU model').get_by_role('radio', name='All', exact=True)).to_be_focused()
    choose(page, 'Platform', 'vLLM')
    page.keyboard.press('Tab')
    expect(group(page, 'Version').get_by_role('radio', name='All versions')).to_be_focused()
    page.keyboard.press('ArrowRight')
    expect(group(page, 'Version').get_by_role('radio', name='0.9', exact=True)).to_be_checked()
    expect(group(page, 'Version').get_by_role('radio', name='0.9', exact=True)).to_be_focused()
    page.keyboard.press('ArrowRight')
    expect(group(page, 'Version').get_by_role('radio', name='0.10', exact=True)).to_be_focused()
    page.keyboard.press('Tab')
    expect(group(page, 'GPU model').get_by_role('radio', name='All', exact=True)).to_be_focused()


def test_no_versions_safe_disabled_and_model_reset(page, served):
    synthetic(page, served)
    # A no-entry family remains safe even if selected programmatically.
    page.evaluate("selectFilter('platform', 'llm-d')")
    expect(group(page, 'Version').locator('input')).to_have_count(1)
    expect(group(page, 'Version').get_by_role('radio', name='All versions')).to_be_disabled()
    expect(page.locator('.platform-unavailable')).to_be_visible()
    expect(page.locator('.recipe-option')).to_have_count(0)
    page.locator('input[name="catalog-model"]').nth(1).check()
    expect(group(page, 'Platform').get_by_role('radio', name='All', exact=True)).to_be_checked()
    expect(group(page, 'Version').get_by_role('radio', name='All versions')).to_be_checked()
    expect(group(page, 'Version').get_by_role('radio', name='All versions')).to_be_disabled()
