"""Always-visible native filter options wrap inside the catalog shell."""
import pytest
from playwright.sync_api import expect


OPTIONS = {
    'scope': ('Scope', ['All', 'Single-node', 'Multi-node']),
    'workload': ('Workload', ['All', '8k/1k', 'Agentic workload']),
    'gpu_model': ('GPU model', ['All', 'B300', 'B200', 'H200', 'H100']),
}


def assert_options_fit(page):
    page.evaluate('document.fonts.ready')
    assert page.evaluate('document.documentElement.scrollWidth <= document.documentElement.clientWidth')
    for key, (label, names) in OPTIONS.items():
        group = page.locator(f'fieldset.filter-options[data-filter="{key}"]')
        expect(group).to_be_visible()
        expect(group).to_have_attribute('role', 'radiogroup')
        expect(group).to_have_attribute('aria-label', label)
        expect(group.locator('legend')).to_have_text(label)
        expect(group.locator('label.filter-option')).to_have_count(len(names))
        for name in names:
            radio = group.get_by_role('radio', name=name, exact=True)
            expect(radio).to_be_visible()
            assert radio.evaluate('el => getComputedStyle(el).appearance') != 'none'
        expect(group.locator('input:checked')).to_have_count(1)
        expect(group.locator('.filter-option--selected')).to_have_count(1)
        assert group.locator('.filter-option').evaluate_all('''options => options.every(option => {
            const box = option.getBoundingClientRect();
            const group = option.closest('fieldset').getBoundingClientRect();
            return box.width > 0 && box.height > 0 && box.left >= group.left
                && box.right <= group.right + 1 && option.scrollWidth <= option.clientWidth;
        })''')


@pytest.mark.parametrize('width', [390, 1280])
def test_single_recipe_gemma_still_shows_all_filter_options(page, served, width):
    page.set_viewport_size({'width': width, 'height': 900})
    payload = served[1]
    model = next(model for model in payload['models']
                 if 'gemma' in model['id'].lower()
                 and sum(entry['model_id'] == model['id'] for entry in payload['entries']) == 1)
    page.goto(served[0])
    page.get_by_role('radio', name=model['name'], exact=True).check()
    expect(page.locator('.recipe-option')).to_have_count(1)
    assert_options_fit(page)
    expect(page.get_by_role('radiogroup', name='Platform', exact=True)).to_be_visible()
    expect(page.get_by_role('radiogroup', name='Version', exact=True)).to_be_visible()

    group = page.locator('[data-filter="gpu_model"]')
    group.get_by_role('radio', name='B300', exact=True).check()
    expect(group.get_by_role('radio', name='B300', exact=True)).to_be_checked()
    expect(page.locator('.recipe-option')).to_have_count(0)
    expect(page.locator('.banner')).to_contain_text('No recipes match')
    assert_options_fit(page)
    selected = group.locator('.filter-option--selected')
    assert selected.evaluate('el => getComputedStyle(el).boxShadow') != 'none'
    page.keyboard.press('Tab')
    selected.locator('input').focus()
    assert selected.evaluate('el => getComputedStyle(el).outlineStyle') != 'none'
