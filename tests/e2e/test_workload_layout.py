"""Workload reference cards and shared navigation fit mobile and desktop."""
import pytest
from playwright.sync_api import expect


@pytest.mark.parametrize('width', [390, 1280])
def test_workload_page_cards_and_navigation_fit(page, served, width):
    page.set_viewport_size({'width': width, 'height': 900})
    response = page.goto(served[0] + '/workloads.html')
    assert response.status == 200
    expect(page.locator('main.workload-page h1')).to_have_text('Workloads')
    cards = page.locator('.workload-grid .workload-card')
    expect(cards).to_have_count(2)
    expect(page.locator('.workload-note')).to_be_visible()
    expect(page.get_by_role('link', name='Model catalog', exact=True)).to_be_visible()
    expect(page.get_by_role('link', name='Workloads', exact=True)).to_have_attribute('aria-current', 'page')
    expect(page.get_by_role('textbox')).to_be_disabled()
    page.evaluate('document.fonts.ready')
    boxes = cards.evaluate_all('cards => cards.map(card => card.getBoundingClientRect().toJSON())')
    if width == 390:
        assert boxes[1]['top'] >= boxes[0]['bottom']
    else:
        assert boxes[0]['top'] == boxes[1]['top']
        assert boxes[1]['left'] >= boxes[0]['right']
    assert_contained(page)
    # Exercise long source URLs without changing the authored page or its links.
    cards.first.evaluate('''card => {
        const link = document.createElement('a');
        link.href = 'https://example.com/source';
        link.textContent = 'https://example.com/source/' + 'x'.repeat(300);
        card.append(link);
    }''')
    assert_contained(page)


def assert_contained(page):
    assert page.evaluate('document.documentElement.scrollWidth <= document.documentElement.clientWidth')
    assert page.locator('.nav, .nav > *, .nav a, .workload-page, .workload-card, .workload-note').evaluate_all('''elements =>
        elements.every(el => {
            const box = el.getBoundingClientRect();
            return box.left >= 0 && box.right <= document.documentElement.clientWidth + 1
                && el.scrollWidth <= el.clientWidth + 1;
        })''')
