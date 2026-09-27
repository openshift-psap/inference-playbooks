# E2E smoke test for the catalog site renderer.
# Verifies that the site loads, renders content, and handles interactions.

import shutil
import subprocess
import sys
import http.server
import threading
import functools
from pathlib import Path
import pytest

try:
    from playwright.sync_api import sync_playwright
    PLAYWRIGHT_AVAILABLE = True
except ImportError:
    PLAYWRIGHT_AVAILABLE = False

REPO = Path(__file__).resolve().parents[2]


@pytest.fixture(scope="session")
def base_url(tmp_path_factory):
    """Serve site/ with a generated catalog.json on a local port."""
    if not PLAYWRIGHT_AVAILABLE:
        pytest.skip("playwright not installed")

    site = tmp_path_factory.mktemp("site")

    # Copy site/ contents to temp directory
    for p in (REPO / "site").iterdir():
        if p.is_dir():
            shutil.copytree(p, site / p.name)
        else:
            shutil.copy(p, site / p.name)

    # Generate catalog.json in the temp site directory
    subprocess.run(
        [sys.executable, "tools/catalog.py", "--out", str(site / "catalog.json")],
        cwd=REPO,
        check=True
    )

    # Serve the temp site on a local port
    port = 8137
    Handler = functools.partial(http.server.SimpleHTTPRequestHandler, directory=str(site))
    httpd = http.server.ThreadingHTTPServer(("127.0.0.1", port), Handler)
    server_thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    server_thread.start()

    yield f"http://127.0.0.1:{port}"

    httpd.shutdown()


def test_unknown_model_falls_back(base_url):
    """Non-existent model query param falls back to first model and renders."""
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page()
        page.goto(base_url + "/?model=does-not-exist")

        # Should render a model card (fallback to first model)
        page.wait_for_selector(".model-card", timeout=5000)
        card_text = page.locator(".model-card").inner_text()
        assert card_text.strip() != "", "Model card should have content"

        browser.close()


def test_configure_tab_shows_flags(base_url):
    """Configure tab displays vLLM flags including --tensor-parallel-size."""
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page()
        page.goto(base_url + "/?model=glm-5.2")

        # Click the Configure tab
        page.wait_for_selector(".tabs", timeout=5000)
        configure_tab = page.locator(".tab", has_text="Configure")
        configure_tab.click()

        # Wait for the Configure panel to render
        page.wait_for_selector(".rht", timeout=5000)

        # Verify --tensor-parallel-size is visible
        # It appears in both the flags table AND the vllm serve drawer,
        # so use .first to avoid strict mode failure
        flag_element = page.locator("text=--tensor-parallel-size").first
        assert flag_element.is_visible(), "--tensor-parallel-size should be visible in Configure tab"

        browser.close()


def test_blocked_scope_button_is_inert(base_url):
    """Clicking a blocked scope button (data-action=noop) doesn't crash the page."""
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page()
        page.goto(base_url + "/?model=glm-5.2")

        # Wait for selectors to render
        page.wait_for_selector(".selectors", timeout=5000)

        # Find a blocked button (they have .btn--blocked class).
        # The catalog always seeds blocked combos, so at least one must render.
        blocked_buttons = page.locator(".btn--blocked")
        assert blocked_buttons.count() > 0, "Catalog should always render at least one blocked button"

        # Click the first blocked button (force=True because it may be disabled)
        blocked_buttons.first.click(force=True)

        # Page should still be consistent - model card still present
        page.wait_for_selector(".model-card", timeout=2000)
        model_card_after = page.locator(".model-card").inner_text()

        # Basic stability check: model card is still there
        assert model_card_after.strip() != "", "Page should remain stable after clicking blocked button"

        browser.close()
