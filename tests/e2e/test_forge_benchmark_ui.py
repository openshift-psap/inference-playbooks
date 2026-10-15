"""Isolated browser proof of Forge evidence presentation; no CDN/network needed."""
import json
from pathlib import Path

import pytest
from playwright.sync_api import expect

REPO = Path(__file__).resolve().parents[2]


@pytest.fixture
def benchmark_page(page):
    page.set_content('<div id="app"></div>')
    page.add_script_tag(content='window.chartConfigs = []; window.Chart = class { constructor(canvas, config) { chartConfigs.push(config); } destroy() {} };')
    page.add_script_tag(content=(REPO / 'site/app.js').read_text())
    return page


def draw(page, sweep, completeness=None):
    page.evaluate('([sweep, completeness]) => { document.getElementById("app").replaceChildren(renderSweepCharts(sweep, completeness)); }',
                  [sweep, completeness])


def test_smoke_stats_without_charts(benchmark_page):
    points = [{'concurrency': c, 'output_tok_s': c * 10, 'ttft_ms_avg': 12} for c in (1, 5)]
    draw(benchmark_page, points, 'smoke')
    expect(benchmark_page.locator('.stat-card')).to_have_count(2)
    expect(benchmark_page.locator('canvas')).to_have_count(0)


@pytest.mark.parametrize('missing_p90', [False, True])
def test_full_sweep_percentile_series(benchmark_page, missing_p90):
    points = json.loads((REPO / 'tests/fixtures/sample-sweep-guidellm.json').read_text())['sweep']
    if missing_p90:
        for point in points:
            point['ttft_ms_p90'] = None
            point['itl_ms_p90'] = None
    draw(benchmark_page, points, 'full-sweep')
    expect(benchmark_page.locator('canvas')).to_have_count(3)
    series = benchmark_page.evaluate('chartConfigs[0].data.datasets')
    assert [s['label'] for s in series] == (['p50', 'p95', 'p99'] if missing_p90 else ['p50', 'p90', 'p95', 'p99'])
    assert next(s for s in series if s['label'] == 'p99')['hidden'] is True
    assert next(s for s in series if s['label'] == 'p50')['hidden'] is False


def test_missing_chart_library_preserves_stats_and_evidence(benchmark_page):
    points = json.loads((REPO / 'tests/fixtures/sample-sweep-guidellm.json').read_text())['sweep']
    errors = []
    benchmark_page.on('pageerror', lambda error: errors.append(str(error)))
    benchmark_page.evaluate('delete window.Chart')
    item = {'run': {'run_id': 'full-sweep', 'harness': 'guidellm'},
            'result': {'metrics': {'output_tok_s': 1120}, 'sweep': points,
                       'completeness': 'full-sweep'}}
    benchmark_page.evaluate('(item) => { document.getElementById("app").replaceChildren(renderBenchmark({validation: {}, evidence: [item]})); }', item)
    expect(benchmark_page.locator('.stat-card').first).to_be_visible()
    expect(benchmark_page.get_by_text('Charts unavailable.', exact=False)).to_be_visible()
    expect(benchmark_page.locator('canvas')).to_have_count(0)
    benchmark_page.get_by_text('Run: full-sweep', exact=True).click()
    expect(benchmark_page.locator('details').get_by_text('1120', exact=True)).to_be_visible()
    assert errors == []


def test_provenance_links_and_no_cross_run_sweep(benchmark_page):
    run = {'run_id': 'test-run', 'harness': 'aiperf', 'environment': {}, 'forge': {
        'mlflow_run_url': 'https://mlflow.example/#/experiments/470/runs/child',
        'mlflow_tracking_uri': 'https://mlflow.example', 'mlflow_experiment_id': '470',
        'mlflow_parent_run_id': 'parent', 'mlflow_workspace': 'forge-sandbox',
        'tested_revision': 'a' * 40, 'completeness': 'smoke'}}
    item = {'run': run, 'result': {'metrics': {}, 'sweep': [
        {'concurrency': c, 'output_tok_s': c * 10} for c in (1, 5)]}}
    benchmark_page.evaluate('(item) => { document.getElementById("app").replaceChildren(renderBenchmark({validation: {}, evidence: [item, item]})); }', item)
    expect(benchmark_page.locator('canvas')).to_have_count(0)
    expect(benchmark_page.locator('.stat-card')).to_have_count(2)
    expect(benchmark_page.get_by_role('link', name='View run in MLflow', include_hidden=True)).to_have_count(2)
    expect(benchmark_page.get_by_role('link', name='View Forge execution', include_hidden=True)).to_have_count(2)
    item['run']['forge']['mlflow_run_url'] = 'javascript:alert(1)'
    item['run']['forge']['mlflow_tracking_uri'] = 'https://user:password@mlflow.example'
    benchmark_page.evaluate('(item) => { document.getElementById("app").replaceChildren(renderEvidenceDetail(item, {validation: {}})); }', item)
    expect(benchmark_page.locator('a')).to_have_count(0)
