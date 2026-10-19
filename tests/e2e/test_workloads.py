"""Workload explanations are accessible, source-backed, and independent of JS."""
from playwright.sync_api import expect


def test_workloads_navigation_and_explanations(page, served):
    page.goto(served[0])
    nav = page.get_by_role('navigation', name='Primary')
    expect(nav.get_by_role('link', name='Model catalog', exact=True)).to_have_attribute('aria-current', 'page')
    nav.get_by_role('link', name='Workloads', exact=True).click()
    expect(page).to_have_url(served[0] + '/workloads.html')
    expect(page.get_by_role('heading', name='Workloads', exact=True)).to_be_visible()
    expect(page.get_by_role('heading', name='8k/1k', exact=True)).to_be_visible()
    expect(page.get_by_role('heading', name='Agentic workload', exact=True)).to_be_visible()
    expect(page.locator('main')).to_contain_text('8,000 prompt tokens and 1,000 output tokens')
    expect(page.locator('main')).to_contain_text('not measured results')
    expect(page.locator('main')).to_contain_text('not infinite model context')
    expect(page.locator('main')).to_contain_text('does not configure the server')
    expect(page.locator('main')).to_contain_text('Unbacked measurements do not establish validation')
    links = page.locator('main a').evaluate_all('links => links.map(link => link.href)')
    assert len(links) == 4
    assert all(link.startswith('https://github.com/openshift-psap/inference-playbooks/blob/main/benchmarks/') for link in links)
    page.get_by_role('navigation', name='Primary').get_by_role('link', name='Model catalog', exact=True).click()
    expect(page.get_by_role('radiogroup', name='Models', exact=True)).to_be_visible()


def test_workload_page_needs_no_javascript_or_catalog(browser, served):
    context = browser.new_context(java_script_enabled=False)
    try:
        page = context.new_page()
        requests = []
        page.on('request', lambda request: requests.append(request.url))
        page.goto(served[0] + '/workloads.html')
        expect(page.get_by_role('heading', name='8k/1k', exact=True)).to_be_visible()
        expect(page.get_by_role('heading', name='Agentic workload', exact=True)).to_be_visible()
        expect(page.get_by_role('navigation', name='Primary').get_by_role('link', name='Workloads', exact=True)).to_have_attribute('aria-current', 'page')
        assert not any(url.endswith(('/catalog.json', '/app.js')) for url in requests)
    finally:
        context.close()
