"""The retained search shell must not imply a working search feature."""
from playwright.sync_api import expect


def test_search_unavailable_and_model_selection_still_works(page, served):
    url, payload = served
    page.goto(url)

    search = page.get_by_role('textbox', name='AI Hub search unavailable', exact=True)
    expect(search).to_be_visible()
    expect(search).to_be_disabled()
    expect(search).to_have_attribute('placeholder', 'Search unavailable')
    expect(search).to_have_attribute('title', 'AI Hub search is not available')

    model = page.locator('input[name="catalog-model"]:checked')
    expect(model).to_be_enabled()
    initial_model = model.input_value()
    target = next(item for item in payload['models'] if item['id'] != initial_model)
    page.get_by_role('radio', name=target['name'], exact=True).check()
    expect(page.locator('input[name="catalog-model"]:checked')).to_have_value(target['id'])
    expect(page.locator('.model-card')).to_contain_text(target['name'])
