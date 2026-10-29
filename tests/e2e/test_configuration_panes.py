"""Responsive manifest-first configuration with source-backed details beside it."""
import copy
from urllib.parse import urlencode

import pytest
from playwright.sync_api import expect


@pytest.mark.parametrize('width', [390, 768, 1199, 1200, 1440, 2560])
def test_configuration_panes_preserve_artifacts_metadata_and_role_arguments(page, served, width):
    page.set_viewport_size({'width': width, 'height': 1000})
    entry = next(entry for entry in served[1]['entries'] if entry['roles'].get('worker'))
    page.goto(served[0] + '/?' + urlencode({'model': entry['model_id'], 'entry': entry['id']}))
    manifests = page.get_by_role('region', name='Deployment manifests', exact=True)
    details = page.get_by_role('region', name='Configuration details', exact=True)
    expect(manifests).to_be_visible()
    expect(details).to_be_visible()
    page.evaluate('document.fonts.ready')
    left, right = [pane.bounding_box() for pane in (manifests, details)]
    if width >= 1200:
        assert abs(left['y'] - right['y']) < 1
        assert left['x'] + left['width'] < right['x']
        assert left['width'] > right['width']
    else:
        assert left['y'] + left['height'] < right['y']
        assert abs(left['x'] - right['x']) < 1
    assert page.evaluate('document.documentElement.scrollWidth <= document.documentElement.clientWidth')
    expect(details.get_by_role('row').filter(has=page.get_by_role('cell', name='Checkpoint', exact=True))).to_contain_text(entry['serving']['model'])
    expect(details.get_by_role('row').filter(has=page.get_by_role('cell', name='Precision (declared)', exact=True))).to_contain_text(entry['quantization'] or 'Not specified')
    expect(details.get_by_role('row').filter(has=page.get_by_role('cell', name='Hardware', exact=True))).to_contain_text(entry['hardware']['data']['accelerators']['model'])
    for role, args in entry['roles'].items():
        if args:
            expect(details.get_by_role('heading', name=f'{role} arguments', exact=True)).to_be_visible()
            for arg in args:
                expect(details).to_contain_text(arg['flag'])
    expect(manifests.locator('.drawer')).to_have_count(len(entry['artifacts']))
    expect(details.locator('.drawer')).to_have_count(0)
    for artifact in entry['artifacts']:
        expect(manifests.locator('pre').filter(has_text=artifact['body'])).to_have_count(1)
    assert manifests.locator('.drawer__code').first.evaluate('el => getComputedStyle(el).overflowX') == 'auto'
    page.get_by_role('button', name='Benchmark', exact=True).click()
    expect(page.locator('.configuration-layout')).to_have_count(0)
    page.get_by_role('button', name='Configuration', exact=True).click()
    expect(page.locator('.configuration-layout')).to_be_visible()


def test_precision_is_never_inferred_from_checkpoint_name(page, served):
    payload = copy.deepcopy(served[1])
    entry = payload['entries'][0]
    entry['serving']['model'] = 'synthetic/checkpoint-FP8'
    entry['quantization'] = None
    page.route('**/catalog.json', lambda route: route.fulfill(json=payload))
    page.goto(served[0] + '/?' + urlencode({'model': entry['model_id'], 'entry': entry['id']}))
    row = page.locator('.configuration-summary tr').filter(has=page.get_by_role('cell', name='Precision (declared)', exact=True))
    expect(row).to_contain_text('Not specified')
    expect(row).not_to_contain_text('FP8')


@pytest.mark.parametrize('parallelism, expected', [
    ({'mode': 'tp+pp', 'tp': 8, 'pp': 2}, ['8 GPUs per tensor-parallel group', '2 pipeline stages']),
    ({'mode': 'tp+dp', 'tp': 2, 'dp': 16}, ['2 GPUs per tensor-parallel group', '16 data-parallel ranks']),
])
def test_configuration_summary_explains_parallelism_without_json_or_schema_pointers(page, served, parallelism, expected):
    payload = copy.deepcopy(served[1])
    entry = payload['entries'][0]
    entry['gpu_allocation']['parallelism'] = parallelism
    entry['serving']['router'] = {'strategy': 'prefill-decode'}
    page.route('**/catalog.json', lambda route: route.fulfill(json=payload))
    page.goto(served[0])
    summary = page.locator('.configuration-summary')
    for text in expected:
        expect(summary).to_contain_text(text)
    expect(summary).to_contain_text('Separate prefill and decode')
    expect(summary).not_to_contain_text('GPUs per replica')
    expect(summary).not_to_contain_text('"mode"')
    expect(summary).not_to_contain_text('recipe.yaml#')
    expect(page.locator('.configuration-details')).to_contain_text('The manifest defines each role.')


def test_technical_details_preserve_source_specs_and_command_caveat(page, served):
    payload = copy.deepcopy(served[1])
    entry = payload['entries'][0]
    entry['specs'] = [
        {'label': 'Declared setting', 'state': 'resolved', 'value': 42, 'source': 'recipe.yaml#/serving/port'},
        {'label': 'Legacy setting', 'state': 'unverified', 'value': None, 'source': 'config/legacy.yaml#/value'},
    ]
    page.route('**/catalog.json', lambda route: route.fulfill(json=payload))
    page.goto(served[0])
    technical = page.locator('.recipe-technical')
    expect(technical).not_to_have_attribute('open', '')
    technical.locator('summary').click()
    expect(technical).to_contain_text(entry['hardware']['path'])
    expect(technical).to_contain_text(f"revision {entry['hardware']['revision']}")
    expect(technical).to_contain_text(entry['serving']['image'])
    expect(technical).to_contain_text(entry['command_note'])
    expect(technical).to_contain_text('Declared setting: 42')
    expect(technical).to_contain_text('Legacy setting: Unverified source note — inspect final artifacts')
    expect(technical).to_contain_text('config/legacy.yaml#/value')
    expect(page.locator('.configuration-summary')).not_to_contain_text('Legacy setting')
