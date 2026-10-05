"""Strict filtering and readable, native-radio recipe selection."""
import copy
from urllib.parse import parse_qs, urlparse

from playwright.sync_api import expect


def open_catalog(page, served, model_id):
    page.goto(f'{served[0]}?model={model_id}')
    expect(page.get_by_role('radiogroup', name='Recipes', exact=True)).to_be_visible()
    return [entry for entry in served[1]['entries'] if entry['model_id'] == model_id]


def test_qwen_singleton_readable_card_and_technical_identity(page, served):
    entries = open_catalog(page, served, 'qwen3-235b-a22b')
    assert len(entries) == 1
    entry = entries[0]
    expect(page.get_by_label('Recipe', exact=True)).to_have_count(0)
    card = page.locator('.recipe-option')
    expect(card).to_have_count(1)
    expect(card).to_contain_text('Agentic workload (128K)')
    expect(card).to_contain_text('16 H200 GPUs per replica (declared)')
    expect(card).to_contain_text('multi-node — rhoai 3.5')
    expect(card).not_to_contain_text(entry['recipe_id'])
    expect(card.locator('input')).to_be_checked()
    expect(card.locator('input')).to_have_value(entry['id'])
    expect(page.locator('.panel table').first).not_to_contain_text(entry['recipe_id'])
    technical = page.locator('.recipe-technical')
    assert technical.get_attribute('open') is None
    technical.locator('summary').click()
    expect(technical).to_contain_text(entry['recipe_id'])
    expect(technical).to_contain_text('No selection rationale supplied')
    if entry['source']['url']:
        expect(technical.get_by_role('link', name='Recipe source')).to_have_attribute('href', entry['source']['url'])
    else:
        expect(technical).to_contain_text(entry['source']['path'])


def test_glm_card_selection_updates_details_artifacts_evidence_and_url(page, served):
    entries = open_catalog(page, served, 'glm-5.2')
    expect(page.locator('.recipe-option')).to_have_count(len(entries))
    assert page.locator('input[name="catalog-recipe"]').evaluate_all('(els) => els.map(el => el.value)') == [entry['id'] for entry in entries]
    page.locator('.recipe-option').last.click()
    for index, entry in enumerate(entries):
        card = page.locator('.recipe-option').nth(index)
        expect(card).to_be_visible()
        expect(card).not_to_contain_text(entry['recipe_id'])
        card.click()
        expect(page.locator('input[name="catalog-recipe"]:checked')).to_have_value(entry['id'])
        expect(page.locator('.recipe-option--selected')).to_have_count(1)
        assert parse_qs(urlparse(page.url).query)['entry'] == [entry['id']]
        expect(page.locator('.model-card')).to_contain_text(entry['maturity'])
        if not entry['blocked']:
            expect(page.locator('.recipe-technical')).to_contain_text(entry['recipe_id'])
            for artifact in entry['artifacts']:
                expect(page.locator('.panel pre').filter(has_text=artifact['body'])).to_have_count(1)
            page.get_by_role('button', name='Benchmark', exact=True).click()
            if not entry['evidence']:
                expect(page.locator('.panel')).to_contain_text('No committed benchmark evidence')
            for item in entry['evidence']:
                expect(page.locator('.panel')).to_contain_text(item['run']['run_id'])
            page.get_by_role('button', name='Configuration', exact=True).click()
    expect(page.get_by_role('radiogroup', name='Workload', exact=True).get_by_role('radio', name='8k/1k', exact=True)).to_be_visible()


def synthetic_catalog(page, served):
    payload = copy.deepcopy(served[1])
    entry = copy.deepcopy(payload['entries'][0])
    entry.update(id='first-entry', model_id=payload['models'][0]['id'], blocked=False,
                 scope='single-node', workload_profile='guidellm-8k1k', notes=None)
    second = copy.deepcopy(entry)
    second.update(id='second-entry', scope='multi-node', workload_profile='aiperf-agentx-unlimited-context')
    payload['entries'] = [entry, second]
    page.route('**/catalog.json', lambda route: route.fulfill(json=payload))
    page.goto(served[0])
    expect(page.locator('.recipe-option')).to_have_count(2)
    return payload


def test_strict_filters_no_results_and_recovery(page, served):
    synthetic_catalog(page, served)
    scope = page.get_by_role('radiogroup', name='Scope', exact=True)
    workload = page.get_by_role('radiogroup', name='Workload', exact=True)
    scope.get_by_role('radio', name='Single-node', exact=True).check()
    workload.get_by_role('radio', name='Agentic workload', exact=True).check()
    expect(scope.get_by_role('radio', name='Single-node', exact=True)).to_be_checked()
    expect(workload.get_by_role('radio', name='Agentic workload', exact=True)).to_be_checked()
    expect(page.locator('.recipe-option')).to_have_count(0)
    expect(page.locator('.banner')).to_contain_text('No recipes match')
    expect(page.locator('.panel')).to_have_count(0)
    assert page.evaluate('state.entryId') is None
    assert 'entry' not in parse_qs(urlparse(page.url).query)
    scope.get_by_role('radio', name='All', exact=True).check()
    expect(page.locator('input[name="catalog-recipe"]:checked')).to_have_value('second-entry')
    expect(page.locator('.recipe-option')).to_have_count(1)


def test_recipe_native_keyboard_focus_and_removed_card_fallback(page, served):
    synthetic_catalog(page, served)
    page.locator('input[name="catalog-recipe"]:checked').focus()
    page.keyboard.press('ArrowRight')
    selected = page.locator('input[name="catalog-recipe"]:checked')
    expect(selected).to_have_value('second-entry')
    expect(selected).to_be_focused()
    page.keyboard.press('ArrowRight')
    expect(selected).to_have_value('first-entry')
    expect(selected).to_be_focused()
    page.evaluate("""() => { selectFilter('scope', 'multi-node'); }""")
    expect(selected).to_have_value('second-entry')
    expect(selected).to_be_focused()
    page.evaluate("""() => { selectFilter('workload', '8k1k'); }""")
    expect(page.locator('input[name="catalog-model"]:checked')).to_be_focused()


def test_hostile_prose_safe_no_invented_recommendation(page, served):
    payload = copy.deepcopy(served[1])
    entry = copy.deepcopy(payload['entries'][0])
    hostile = '<img src=x onerror="window.injected=true">'
    entry.update(id='hostile-entry', model_id=payload['models'][0]['id'], blocked=False,
                 workload_profile=hostile, notes={'profile': {'label': hostile, 'headline': '<script>window.injected=true</script>'},
                                                  'default_stance': 'Source rationale only'})
    entry['notes_source'] = {'path': 'notes.yaml', 'url': 'https://example.com/notes.yaml'}
    payload['entries'] = [entry]
    page.route('**/catalog.json', lambda route: route.fulfill(json=payload))
    page.goto(served[0])
    card = page.locator('.recipe-option')
    expect(card).to_contain_text(hostile)
    expect(card).not_to_contain_text('Source rationale only')
    text = card.inner_text().lower()
    assert all(word not in text for word in ['fastest', 'recommended', 'sizing', 'benchmark'])
    technical = page.locator('.recipe-technical')
    technical.locator('summary').click()
    expect(technical).to_contain_text('Source rationale only')
    expect(technical).to_contain_text('not a measured recommendation')
    expect(technical.get_by_role('link', name='Selection rationale source')).to_have_attribute('href', 'https://example.com/notes.yaml')
    expect(page.locator('#app img, #app script')).to_have_count(0)
    assert page.evaluate('window.injected === undefined')
