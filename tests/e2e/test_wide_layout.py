"""Fluid shells use wide viewports while filters and metadata remain compact."""
import pytest
from playwright.sync_api import expect


def assert_document_fits(page):
    page.evaluate('document.fonts.ready')
    assert page.evaluate('document.documentElement.scrollWidth <= document.documentElement.clientWidth')


@pytest.mark.parametrize('width', [390, 1280, 2560])
def test_catalog_fluid_width_filter_pair_and_compact_metadata(page, served, width):
    page.set_viewport_size({'width': width, 'height': 1000})
    page.goto(served[0])
    layout = page.locator('.catalog-layout')
    expect(layout).to_be_visible()
    expect(page.locator('.model-meta')).to_be_visible()
    assert_document_fits(page)
    assert layout.evaluate('el => el.getBoundingClientRect().width') >= width * .9
    assert page.locator('.selectors').evaluate('el => el.getBoundingClientRect().width') >= width * .8
    pair = page.locator('.filter-pair')
    expect(pair.locator('fieldset')).to_have_count(2)
    boxes = pair.locator('fieldset').evaluate_all('fields => fields.map(el => el.getBoundingClientRect().toJSON())')
    if width == 390:
        assert boxes[1]['top'] >= boxes[0]['bottom']
    else:
        assert boxes[0]['top'] == boxes[1]['top']
        assert boxes[1]['left'] >= boxes[0]['right']
        assert page.locator('.model-meta').evaluate('el => el.getBoundingClientRect().height') <= 48
        assert page.locator('.model-card').evaluate('el => el.getBoundingClientRect().height') <= 220
    assert page.locator('.model-meta').evaluate('el => el.scrollWidth <= el.clientWidth')
    assert page.locator('.drawer__code').first.evaluate('el => getComputedStyle(el).overflowX') == 'auto'


def test_catalog_and_workloads_grow_with_viewport(page, served):
    for path, selector in [('/', '.catalog-layout .selectors'), ('/workloads.html', '.workload-grid')]:
        widths = []
        for width in [1280, 2560]:
            page.set_viewport_size({'width': width, 'height': 1000})
            page.goto(served[0] + path)
            content = page.locator(selector)
            expect(content).to_be_visible()
            assert_document_fits(page)
            content_width = content.evaluate('el => el.getBoundingClientRect().width')
            assert content_width >= width * .9
            widths.append(content_width)
        assert widths[1] > widths[0] * 1.8
