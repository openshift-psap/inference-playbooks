"""Responsive containment and visible selection proof for model radio cards."""
import copy

import pytest
from playwright.sync_api import expect


@pytest.mark.parametrize('width', [390, 1280])
def test_model_cards_wrap_without_hiding_choices(page, served, width):
    page.set_viewport_size({'width': width, 'height': 900})
    payload = copy.deepcopy(served[1])
    original = payload['models'][0]
    # Extra choices stress wrapping independently of the current catalog size.
    for index in range(6):
        model = copy.deepcopy(original)
        model['id'] = f'layout-model-{index}-' + 'x' * 120
        model['name'] = f'Long model {index} ' + 'UnbrokenModelName' * 16
        payload['models'].append(model)
    page.route('**/catalog.json', lambda route: route.fulfill(json=payload))
    page.goto(served[0])
    cards = page.locator('fieldset.model-options label.model-option')
    expect(cards).to_have_count(len(payload['models']))
    page.evaluate('document.fonts.ready')

    assert page.evaluate('document.documentElement.scrollWidth <= document.documentElement.clientWidth')
    assert cards.evaluate_all('''cards => cards.every(card => {
        const box = card.getBoundingClientRect();
        const group = card.closest('fieldset').getBoundingClientRect();
        return box.width > 0 && box.height > 0 && box.left >= group.left
            && box.right <= group.right + 1 && card.scrollWidth <= card.clientWidth;
    })''')
    for model in payload['models']:
        card = cards.filter(has=page.get_by_role('radio', name=model['name'], exact=True))
        expect(card).to_be_visible()
        expect(card.locator('.model-option__name')).to_have_text(model['name'])
        expect(card.locator('.model-option__id')).to_have_text(model['id'])
        for selector in ['.model-option__name', '.model-option__id']:
            assert card.locator(selector).evaluate('''el => {
                const style = getComputedStyle(el);
                return el.scrollHeight <= el.clientHeight && el.scrollWidth <= el.clientWidth
                    && style.textOverflow !== 'ellipsis' && style.overflow !== 'hidden';
            }''')
    positions = cards.evaluate_all('cards => cards.map(card => card.getBoundingClientRect().top)')
    assert len(set(positions)) > 1
    if width == 1280:
        assert len(set(positions)) < len(positions)


def test_model_card_selection_and_keyboard_focus_are_visible(page, served):
    page.goto(served[0])
    radios = page.locator('.model-option input[type=radio]')
    expect(radios.first).to_be_visible()
    selected = page.locator('.model-option--selected')
    expect(selected).to_have_count(1)
    radio = selected.locator('input[type=radio]')
    expect(radio).to_be_checked()
    assert radio.evaluate('el => getComputedStyle(el).appearance') != 'none'
    assert selected.evaluate('el => getComputedStyle(el).boxShadow') != 'none'
    page.keyboard.press('Tab')
    radio.focus()
    expect(radio).to_be_focused()
    assert selected.evaluate('el => getComputedStyle(el).outlineStyle') != 'none'
