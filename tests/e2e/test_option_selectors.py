"""Fixed visible option radios filter only explicit catalog facts."""
import copy

import pytest
from playwright.sync_api import expect


OPTIONS = {'GPU model': ['All', 'B300', 'B200', 'H200', 'H100'],
           'Scope': ['All', 'Single-node', 'Multi-node'],
           'Workload': ['All', '8k/1k', 'Agentic workload']}


def group(page, label):
    return page.get_by_role('radiogroup', name=label, exact=True)


def choose(page, label, option):
    group(page, label).get_by_role('radio', name=option, exact=True).check()


def assert_single_checked(page):
    for label in OPTIONS:
        expect(group(page, label).locator('input:checked')).to_have_count(1)
        expect(group(page, label).locator('.filter-option--selected')).to_have_count(1)


@pytest.mark.parametrize('model', ['gemma-4', 'qwen3-235b-a22b'])
def test_fixed_groups_always_visible_on_single_recipe_models(page, served, model):
    page.goto(f'{served[0]}?model={model}')
    for label, options in OPTIONS.items():
        expect(group(page, label)).to_be_visible()
        assert group(page, label).locator('label span').all_text_contents() == options
        for option in options:
            expect(group(page, label).get_by_role('radio', name=option, exact=True)).to_be_visible()
        expect(group(page, label).get_by_role('radio', name='All', exact=True)).to_be_checked()
    assert_single_checked(page)
    expect(page.get_by_role('radiogroup', name='Platform', exact=True)).to_be_visible()
    expect(page.get_by_role('radiogroup', name='Version', exact=True)).to_be_visible()
    expect(page.locator('select[aria-label="Accelerators"], select[aria-label="Workload"], select[aria-label="Scope"]')).to_have_count(0)


def synthetic(page, served):
    payload = copy.deepcopy(served[1])
    base = copy.deepcopy(payload['entries'][0])
    payload['entries'] = []
    for index, (gpu, scope, workload) in enumerate([
        ('h200', 'single-node', 'guidellm-8k1k'),
        ('H100', 'multi-node', 'aiperf-agentx-128k'),
        ('B200', 'single-node', 'aiperf-agentx-unlimited-context'),
        ('H200', 'multi-node', 'unknown-agentic-profile'),
    ]):
        entry = copy.deepcopy(base)
        entry.update(id=f'option-{index}', model_id=payload['models'][0]['id'], scope=scope, workload_profile=workload, blocked=False)
        entry['hardware']['data']['accelerators']['model'] = gpu
        entry['hardware']['accelerator_key'] = 'nvidia-b300-x8'
        entry['hardware']['path'] = 'hardware-profiles/b300.yaml'
        payload['entries'].append(entry)
    page.route('**/catalog.json', lambda route: route.fulfill(json=payload))
    page.goto(served[0])
    expect(page.locator('.recipe-option')).to_have_count(4)


def recipe_values(page):
    return page.locator('input[name="catalog-recipe"]').evaluate_all('(els) => els.map(el => el.value)')


def test_all_union_and_exact_workload_groups(page, served):
    synthetic(page, served)
    assert recipe_values(page) == ['option-0', 'option-1', 'option-2', 'option-3']
    choose(page, 'Workload', 'Agentic workload')
    assert recipe_values(page) == ['option-1', 'option-2']
    choose(page, 'Workload', '8k/1k')
    assert recipe_values(page) == ['option-0']
    choose(page, 'Workload', 'All')
    choose(page, 'Scope', 'Multi-node')
    assert recipe_values(page) == ['option-1', 'option-3']
    choose(page, 'Scope', 'All')
    assert len(recipe_values(page)) == 4
    assert_single_checked(page)


def test_gpu_explicit_model_only_no_phantom_and_strict_recovery(page, served):
    synthetic(page, served)
    choose(page, 'GPU model', 'H200')
    assert recipe_values(page) == ['option-0', 'option-3']
    choose(page, 'GPU model', 'B300')
    expect(page.locator('.recipe-option')).to_have_count(0)
    expect(page.locator('.banner')).to_contain_text('No recipes match')
    assert page.evaluate('state.entryId') is None
    assert_single_checked(page)
    choose(page, 'Scope', 'Multi-node')
    choose(page, 'GPU model', 'B200')
    expect(page.locator('.recipe-option')).to_have_count(0)
    expect(group(page, 'Scope').get_by_role('radio', name='Multi-node', exact=True)).to_be_checked()
    choose(page, 'Scope', 'All')
    assert recipe_values(page) == ['option-2']
    expect(page.locator('input[name="catalog-recipe"]:checked')).to_have_value('option-2')
    choose(page, 'GPU model', 'All')
    assert len(recipe_values(page)) == 4
    assert_single_checked(page)


def test_keyboard_focus_restored_through_no_results_and_group_order(page, served):
    synthetic(page, served)
    page.get_by_role('radiogroup', name='Platform', exact=True).get_by_role('radio', name='All', exact=True).focus()
    page.keyboard.press('Tab')
    expect(group(page, 'GPU model').get_by_role('radio', name='All', exact=True)).to_be_focused()
    page.keyboard.press('ArrowRight')
    b300 = group(page, 'GPU model').get_by_role('radio', name='B300', exact=True)
    expect(b300).to_be_checked()
    expect(b300).to_be_focused()
    expect(b300).to_have_attribute('data-focus-key', 'filter:gpu_model:B300')
    page.keyboard.press('ArrowLeft')
    expect(group(page, 'GPU model').get_by_role('radio', name='All', exact=True)).to_be_focused()
    expect(page.locator('.recipe-option')).to_have_count(4)
    page.keyboard.press('Tab')
    expect(group(page, 'Scope').get_by_role('radio', name='All', exact=True)).to_be_focused()
    page.keyboard.press('ArrowRight')
    expect(group(page, 'Scope').get_by_role('radio', name='Single-node', exact=True)).to_be_focused()
    page.keyboard.press('Tab')
    expect(group(page, 'Workload').get_by_role('radio', name='All', exact=True)).to_be_focused()
    page.keyboard.press('ArrowRight')
    expect(group(page, 'Workload').get_by_role('radio', name='8k/1k', exact=True)).to_be_checked()
    expect(group(page, 'Workload').get_by_role('radio', name='8k/1k', exact=True)).to_be_focused()
    assert_single_checked(page)
