"""Recipe choices remain readable and contained, including strict empty filters."""
import copy

import pytest
from playwright.sync_api import expect


def assert_contained(page):
    page.evaluate('document.fonts.ready')
    assert page.evaluate('document.documentElement.scrollWidth <= document.documentElement.clientWidth')
    assert page.locator('.recipe-option').evaluate_all('''cards => cards.every(card => {
        const box = card.getBoundingClientRect();
        const group = card.closest('fieldset').getBoundingClientRect();
        return box.width > 0 && box.left >= group.left && box.right <= group.right + 1
            && card.scrollWidth <= card.clientWidth && [...card.children].every(child => {
                const style = getComputedStyle(child);
                return child.scrollWidth <= child.clientWidth
                    && child.scrollHeight <= child.clientHeight
                    && style.textOverflow !== 'ellipsis' && style.overflow !== 'hidden';
            });
    })''')


@pytest.mark.parametrize('width', [390, 1280])
def test_glm_recipe_cards_fit_and_show_selection(page, served, width):
    page.set_viewport_size({'width': width, 'height': 900})
    model = next(model for model in served[1]['models'] if model['name'] == 'GLM-5.2')
    entries = [entry for entry in served[1]['entries'] if entry['model_id'] == model['id']]
    assert 1 < len(entries) <= 10
    page.goto(served[0])
    page.get_by_role('radio', name=model['name'], exact=True).check()
    cards = page.locator('.recipe-option')
    expect(cards).to_have_count(len(entries))
    for card in cards.all():
        expect(card).to_be_visible()
    assert_contained(page)
    positions = cards.evaluate_all('cards => cards.map(card => card.getBoundingClientRect().top)')
    if width == 390:
        assert len(set(positions)) == len(entries)
    else:
        assert len(set(positions)) < len(entries)
    selected = page.locator('.recipe-option--selected')
    expect(selected).to_have_count(1)
    radio = selected.locator('input[type=radio]')
    expect(radio).to_be_checked()
    assert radio.evaluate('el => getComputedStyle(el).appearance') != 'none'
    assert selected.evaluate('el => getComputedStyle(el).boxShadow') != 'none'
    page.keyboard.press('Tab')
    radio.focus()
    assert selected.evaluate('el => getComputedStyle(el).outlineStyle') != 'none'


@pytest.mark.parametrize('width', [390, 1280])
def test_long_recipe_text_and_strict_empty_filters_fit(page, served, width):
    page.set_viewport_size({'width': width, 'height': 900})
    payload = copy.deepcopy(served[1])
    model = next(model for model in payload['models'] if model['name'] == 'GLM-5.2')
    original = next(entry for entry in payload['entries'] if entry['model_id'] == model['id'])
    long_text = 'LongUnbrokenWorkload' * 20
    entries = []
    for index, scope in enumerate(['single-node', 'multi-node']):
        entry = copy.deepcopy(original)
        entry['id'] = f'layout-recipe-{index}'
        entry['scope'] = scope
        entry['workload_profile'] = long_text + str(index)
        entries.append(entry)
    payload['entries'] = entries
    page.route('**/catalog.json', lambda route: route.fulfill(json=payload))
    page.goto(served[0])
    page.get_by_role('radio', name=model['name'], exact=True).check()
    expect(page.locator('.recipe-option')).to_have_count(2)
    expect(page.locator('.recipe-options')).to_contain_text(long_text)
    assert_contained(page)
    scope = page.get_by_role('radiogroup', name='Scope', exact=True)
    gpu = page.get_by_role('radiogroup', name='GPU model', exact=True)
    scope.get_by_role('radio', name='Single-node', exact=True).check()
    gpu.get_by_role('radio', name='B300', exact=True).check()
    expect(page.locator('.recipe-option')).to_have_count(0)
    expect(scope.get_by_role('radio', name='Single-node', exact=True)).to_be_checked()
    expect(gpu.get_by_role('radio', name='B300', exact=True)).to_be_checked()
    expect(page.locator('.banner')).to_have_text('No recipes match this model and selection.')
    assert_contained(page)
