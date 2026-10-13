"""Browser proof against generated v4 JSON and deliberately hostile fixtures."""
import copy
from urllib.parse import urlencode

from playwright.sync_api import expect


def intercepted(page, served, payload):
    page.route('**/catalog.json', lambda route: route.fulfill(json=payload))
    page.goto(served[0])


def test_all_recipe_platform_entries_reachable(page, served):
    url, payload = served
    assert len(payload['entries']) == 6
    for entry in payload['entries']:
        page.goto(url + '/?' + urlencode({'model': entry['model_id'], 'entry': entry['id']}))
        expect(page.locator('input[name="catalog-recipe"]:checked')).to_have_value(entry['id'])
        expect(page.locator('.model-card')).to_contain_text(f"Recipe maturity: {entry['maturity']}")
        for artifact in entry['artifacts']:
            expect(page.locator('pre').filter(has_text=artifact['body']).first).to_be_visible()
        page.get_by_role('button', name='Benchmark', exact=True).click()
        expect(page.locator('.panel')).to_contain_text('No committed benchmark evidence for this recipe')


def test_invalid_urls_model_switch_and_ambiguity(page, served):
    page.goto(served[0] + '/?model=missing&entry=invalid')
    expect(page.locator('.model-card')).to_be_visible()
    page.get_by_role('radio', name='GLM-5.2', exact=True).check()
    assert page.locator('input[name="catalog-recipe"]').count() == 4
    page.get_by_role('radiogroup', name='Scope', exact=True).get_by_role('radio', name='Single-node', exact=True).check()
    assert page.locator('input[name="catalog-recipe"]').count() == 2
    expect(page.locator('.model-card')).to_contain_text('contributed')
    model = next(model for model in served[1]['models'] if model['id'] == 'gemma-4')
    page.get_by_role('radio', name=model['name'], exact=True).check()
    expect(page.locator('.panel')).to_contain_text('1 GPUs')
    expect(page.locator('.panel')).to_contain_text('8 accelerators')


def test_empty_catalog_and_model_without_recipes(page, served):
    payload = copy.deepcopy(served[1]); payload['models'] = []; payload['entries'] = []
    intercepted(page, served, payload)
    expect(page.locator('#app')).to_contain_text('No models are available')
    payload['models'] = [copy.deepcopy(served[1]['models'][0])]
    payload['models'][0]['entry_ids'] = []
    page.goto(served[0])
    expect(page.locator('#app')).to_contain_text('No recipes match this model')


def test_blocked_entry_reason_not_fake_option_universe(page, served):
    payload = copy.deepcopy(served[1])
    entry = payload['entries'][0]; entry['blocked'] = True; entry['reason'] = 'Synthetic missing prerequisite'
    intercepted(page, served, payload)
    page.locator('input[name="catalog-recipe"]').first.check()
    expect(page.locator('.banner--pending')).to_contain_text(entry['reason'])
    assert page.locator('.drawer').count() == 0


def hostile_payload(served):
    payload = copy.deepcopy(served[1])
    payload['models'][0]['name'] = '<img src=x onerror="window.PWNED=true"> "hostile"'
    entry = payload['entries'][0]
    entry['artifacts'][0]['body'] = '\ufeff# <PLACEHOLDER> "quotes" $(do-not-run)\r\nkind: Deployment\r\n'
    entry['source']['url'] = 'javascript:window.PWNED=true'
    return payload, entry


def test_hostile_text_and_exact_copy_download(page, served, tmp_path):
    payload, entry = hostile_payload(served)
    page.add_init_script("Object.defineProperty(navigator, 'clipboard', {value: {writeText: async text => {window.copiedText = text;}}});")
    intercepted(page, served, payload)
    expect(page.locator('.model-card')).to_contain_text('<img src=x')
    assert page.locator('img').count() == 0
    assert page.locator('a[href^="javascript:"]').count() == 0
    assert page.evaluate('window.PWNED') is None
    drawer = page.locator('.drawer').first
    drawer.get_by_role('button', name='Copy', exact=True).click()
    expect(drawer.get_by_role('button', name='Copied', exact=True)).to_be_visible()
    assert page.evaluate('window.copiedText') == entry['artifacts'][0]['body']
    with page.expect_download() as download:
        drawer.get_by_role('button', name='Download', exact=True).click()
    destination = tmp_path / 'download.yaml'
    download.value.save_as(destination)
    assert destination.read_bytes() == entry['artifacts'][0]['body'].encode('utf-8')


def test_clipboard_rejection_is_not_reported_as_copied(page, served):
    page.add_init_script("Object.defineProperty(navigator, 'clipboard', {value: {writeText: async () => {throw new Error('denied');}}});")
    page.goto(served[0])
    page.get_by_role('button', name='Copy', exact=True).first.click()
    expect(page.get_by_role('button', name='Copy failed — use Download')).to_be_visible()
    assert page.get_by_role('button', name='Copied', exact=True).count() == 0


def test_metrics_preserve_zero_unknown_units_and_unfamiliar_fields(page, served):
    payload = copy.deepcopy(served[1]); entry = payload['entries'][0]
    entry['evidence'] = [{'run': {'run_id': 'synthetic-run'}, 'result': {'metrics': {
        'zero': {'value': 0, 'unit': None, 'statistic': 'unknown-statistic', 'unfamiliar': 'kept'}, 'missing': None}},
        'source': {'path': 'synthetic/run.yaml', 'url': None}, 'result_source': {'path': 'synthetic/result.json', 'url': None},
        'run_body': 'run_id: synthetic-run\n', 'result_body': '{"synthetic":true}\n'}]
    intercepted(page, served, payload)
    page.get_by_role('button', name='Benchmark', exact=True).click()
    expect(page.locator('.panel')).to_contain_text('"value":0')
    expect(page.locator('.panel')).to_contain_text('"unit":null')
    expect(page.locator('.panel')).to_contain_text('unknown-statistic')
    expect(page.locator('.panel')).to_contain_text('unfamiliar')
    expect(page.locator('.panel')).to_contain_text('Unknown')
