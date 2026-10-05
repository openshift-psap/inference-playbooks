"""Focused regression proof for catalog containment and selector labels."""
import copy
from urllib.parse import urlencode

from playwright.sync_api import expect


def assert_document_fits(page):
    assert page.evaluate('document.documentElement.scrollWidth') <= page.evaluate(
        'document.documentElement.clientWidth'
    )


def assert_selectors_fit(page):
    assert page.locator('.sel-row select').evaluate_all('''selects => selects.every(select => {
        const box = select.getBoundingClientRect();
        const shell = select.closest('.selectors').getBoundingClientRect();
        return box.left >= shell.left && box.right <= shell.right;
    })''')


def test_mobile_catalog_entries_and_dropdowns_fit(page, served):
    page.set_viewport_size({'width': 390, 'height': 844})
    url, payload = served
    for entry in payload['entries']:
        page.goto(url + '/?' + urlencode({'model': entry['model_id'], 'entry': entry['id']}))
        expect(page.locator('input[name="catalog-recipe"]:checked')).to_have_value(entry['id'])
        page.evaluate('document.fonts.ready')
        assert_document_fits(page)
        assert_selectors_fit(page)
    # Also exercise re-rendering through visible model choices, not just deep links.
    for model in payload['models']:
        page.get_by_role('radio', name=model['name'], exact=True).check()
        expect(page.locator('.model-card')).to_be_visible()
        assert_document_fits(page)
        assert_selectors_fit(page)


def test_mobile_long_content_wraps_and_code_scrolls_locally(page, served):
    page.set_viewport_size({'width': 390, 'height': 844})
    payload = copy.deepcopy(served[1])
    entry = payload['entries'][0]
    long_text = 'unbroken-source-path-' + 'x' * 240
    model = next(model for model in payload['models'] if model['id'] == entry['model_id'])
    model['name'] = long_text
    entry['recipe_id'] = long_text
    entry['serving']['image'] = long_text
    entry['command_note'] = long_text
    entry['source'] = {'path': long_text, 'url': None}
    entry['artifacts'][0]['name'] = long_text + '.yaml'
    entry['artifacts'][0]['body'] = long_text + '\n'
    entry['notes'] = {'quickstart': [{'step': long_text, 'detail': long_text}]}
    entry['notes_source'] = {'path': long_text, 'url': None}
    page.route('**/catalog.json', lambda route: route.fulfill(json=payload))
    page.goto(served[0] + '/?' + urlencode({'model': model['id'], 'entry': entry['id']}))
    expect(page.locator('.model-card')).to_contain_text(long_text)
    page.evaluate('document.fonts.ready')
    assert_document_fits(page)
    assert_selectors_fit(page)
    expect(page.locator('.rht').first).to_contain_text(long_text)
    expect(page.locator('.panel')).to_contain_text(long_text + ' (local preview)')
    code = page.locator('.drawer__code').first
    assert code.evaluate('el => el.scrollWidth > el.clientWidth')
    assert code.evaluate('el => getComputedStyle(el).overflowX') == 'auto'
    page.get_by_role('button', name='Quick start', exact=True).click()
    expect(page.locator('.panel h2')).to_have_text(long_text)
    assert_document_fits(page)


def test_desktop_version_toggles_fit_their_group(page, served):
    page.set_viewport_size({'width': 1280, 'height': 900})
    page.goto(served[0])
    page.get_by_role('radiogroup', name='Platform', exact=True).get_by_role('radio', name='vLLM', exact=True).check()
    group = page.get_by_role('radiogroup', name='Version', exact=True)
    expect(group).to_be_visible()
    page.evaluate('document.fonts.ready')
    assert group.locator('.filter-option').evaluate_all('''options => options.every(option => {
        const box = option.getBoundingClientRect();
        const group = option.closest('fieldset').getBoundingClientRect();
        return box.left >= group.left && box.right <= group.right;
    })''')
    assert_document_fits(page)
    assert_selectors_fit(page)
