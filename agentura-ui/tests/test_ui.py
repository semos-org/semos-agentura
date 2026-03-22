"""Browser tests for agentura-ui using Playwright.

Run with:
    uv run pytest agentura-ui/tests/test_ui.py -v

Requires: pip install pytest-playwright && playwright install chromium
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

import panel as pn
from playwright.sync_api import Page, expect
from panel.tests.util import serve_component, wait_until

_TEST_CONFIG = """\
providers:
  test:
    display_name: "Test Provider"
    client_type: "anthropic"
    env_vars:
      api_key: "${_TEST_API_KEY}"
      endpoint: "${_TEST_ENDPOINT}"
    models:
      - name: "Test Model"
        value: "anthropic/test-model"
"""

PORT = [6200]


@pn.cache
def _test_config_path():
    """Write test config once and return path."""
    os.environ.setdefault("_TEST_API_KEY", "dummy-key")
    os.environ.setdefault("_TEST_ENDPOINT", "http://localhost:9999")
    d = Path(tempfile.mkdtemp())
    p = d / "config.yml"
    p.write_text(_TEST_CONFIG)
    return p


# Smoke test: Panel + Playwright work together


def test_panel_serve_smoke(page: Page):
    """Basic smoke: serve a Panel pane and find it."""
    PORT[0] += 1
    md = pn.pane.Markdown("# Hello Panel Test")
    serve_component(page, md)
    expect(page.locator("text=Hello Panel Test")).to_be_visible(
        timeout=10000,
    )


# Panelini AI Frontend tests


def test_ui_sidebar_has_tools(page: Page):
    """Sidebar renders tool checkboxes from AVAILABLE_TOOLS."""
    PORT[0] += 1
    from panelini.components.ai.frontend import Frontend

    frontend = Frontend(
        system_message="Test",
        config_path=_test_config_path(),
    )

    # Serve just the sidebar card (lighter than full Panelini)
    sidebar_col = pn.Column(*frontend.sidebar_objects)
    serve_component(page, sidebar_col)

    expect(
        page.locator("text=Get Current Time"),
    ).to_be_visible(timeout=10000)


def test_ui_chat_interface_renders(page: Page):
    """Chat interface renders with input area."""
    PORT[0] += 1
    from panelini.components.ai.frontend import Frontend

    frontend = Frontend(
        system_message="Test",
        config_path=_test_config_path(),
    )

    # Serve just the main content (chat + preview)
    main_col = pn.Column(*frontend.main_objects)
    serve_component(page, main_col)

    # ChatInterface renders a welcome message from the assistant
    wait_until(
        lambda: page.locator("text=Assistant").first.is_visible(),
        page,
        timeout=15000,
    )
