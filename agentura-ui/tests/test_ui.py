"""Browser tests for agentura-ui using Playwright.

Run with:
    uv run pytest agentura-ui/tests/test_ui.py -v
    uv run pytest agentura-ui/tests/test_ui.py -v --headed  # visible browser

Requires: pytest-playwright + playwright install chromium
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

import panel as pn
from playwright.sync_api import Page, expect
from panel.tests.util import serve_component, wait_until

from agentura_ui.file_registry import FileRegistry


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


@pn.cache
def _test_config_path():
    """Write test config once and return path."""
    os.environ.setdefault("_TEST_API_KEY", "dummy-key")
    os.environ.setdefault("_TEST_ENDPOINT", "http://localhost:9999")
    d = Path(tempfile.mkdtemp())
    p = d / "config.yml"
    p.write_text(_TEST_CONFIG)
    return p


# Smoke tests

def test_panel_serve_smoke(page: Page):
    """Basic smoke: serve a Panel pane and find it."""
    md = pn.pane.Markdown("# Hello Panel Test")
    serve_component(page, md)
    expect(page.locator("text=Hello Panel Test")).to_be_visible(
        timeout=10000,
    )


def test_ui_sidebar_has_tools(page: Page):
    """Sidebar renders tool checkboxes from AVAILABLE_TOOLS."""
    from panelini.panels.ai.frontend import AiChat as Frontend

    frontend = Frontend(
        system_message="Test",
        config_path=_test_config_path(),
    )
    sidebar_col = pn.Column(*frontend.sidebar_objects)
    serve_component(page, sidebar_col)

    expect(
        page.locator("text=Get Current Time"),
    ).to_be_visible(timeout=10000)


def test_ui_chat_interface_renders(page: Page):
    """Chat interface renders with input area."""
    from panelini.panels.ai.frontend import AiChat as Frontend

    frontend = Frontend(
        system_message="Test",
        config_path=_test_config_path(),
    )
    main_col = pn.Column(*frontend.main_objects)
    serve_component(page, main_col)

    wait_until(
        lambda: page.locator("text=Assistant").first.is_visible(),
        page,
        timeout=15000,
    )


# Full app test (no real agents - uses fake LangChain tools)

import pytest  # noqa: E402


@pytest.fixture()
def full_app(page: Page):
    """Launch full Panelini app with fake tools (no agents)."""
    from langchain_core.tools import BaseTool
    from panelini.panels.ai.frontend import (
        AVAILABLE_TOOLS,
        AiChat as Frontend,
    )

    from agentura_ui.__main__ import _wrap_chat_callback
    from agentura_ui.file_manager import FileManager

    registry = FileRegistry()

    # Fake tools that don't need real agents
    class FakeSearch(BaseTool):
        name: str = "search_emails"
        description: str = "Search emails"

        def _run(self, **kw):
            return '[{"subject": "Test email"}]'

    class FakeDigest(BaseTool):
        name: str = "digest_document"
        description: str = "Digest a document"

        def _run(self, **kw):
            return "# Mock digest"

    class FakeAskEmail(BaseTool):
        name: str = "ask_email_agent"
        description: str = "Ask the email agent"

        def _run(self, **kw):
            return "Mock email response"

    AVAILABLE_TOOLS.clear()
    AVAILABLE_TOOLS.extend([
        FakeSearch(), FakeDigest(), FakeAskEmail(),
    ])

    frontend = Frontend(
        system_message="Test assistant.",
        config_path=_test_config_path(),
    )

    # Enable all tools
    for info in frontend.tool_checkboxes.values():
        info["checkbox"].value = True
    frontend.backend.update_tools(
        frontend._get_selected_tools(),
    )

    pending: list[str] = []
    file_mgr = FileManager(registry, pending)
    frontend.chat_interface.callback = _wrap_chat_callback(
        frontend.chat_interface.callback,
        registry, pending, file_mgr,
    )

    # Serve sidebar + main as a simple layout (Panelini
    # wraps Bokeh which doesn't play well with serve_component)
    layout = pn.Row(
        pn.Column(
            *frontend.sidebar_objects,
            file_mgr.panel,
            width=300,
        ),
        pn.Column(*frontend.main_objects),
        sizing_mode="stretch_both",
    )

    serve_component(page, layout)

    yield {"page": page, "frontend": frontend, "registry": registry}

    AVAILABLE_TOOLS.clear()


def test_full_app_loads_with_tools(full_app):
    """Full app renders tool checkboxes and chat area."""
    page = full_app["page"]

    # Wait for page to load - look for the chat textarea
    textarea = page.locator("textarea")
    expect(textarea.first).to_be_visible(timeout=20000)

    # Tool checkboxes rendered (3 fake + built-in)
    expect(
        page.locator("text=Search Emails"),
    ).to_be_visible(timeout=10000)
    expect(
        page.locator("text=Ask Email Agent"),
    ).to_be_visible(timeout=5000)


def test_full_app_send_message(full_app):
    """Type a message and press Enter - user bubble appears."""
    page = full_app["page"]

    textarea = page.locator("textarea").first
    expect(textarea).to_be_visible(timeout=20000)
    textarea.fill("Hello test assistant")
    textarea.press("Enter")

    expect(
        page.locator("text=Hello test assistant"),
    ).to_be_visible(timeout=15000)


@pytest.fixture()
def tool_roundtrip_app(page: Page):
    """App with a fake callback that simulates LLM calling a tool
    and returning a response with the tool result."""
    from langchain_core.tools import BaseTool
    from panelini.panels.ai.frontend import (
        AVAILABLE_TOOLS,
        AiChat as Frontend,
    )

    from agentura_ui.file_manager import FileManager

    registry = FileRegistry()

    class FakeSearch(BaseTool):
        name: str = "search_emails"
        description: str = "Search emails by keyword"

        def _run(self, query: str = "", **kw):
            return (
                '[{"subject": "Meeting tomorrow",'
                ' "from": "alice@test.com"}]'
            )

    AVAILABLE_TOOLS.clear()
    AVAILABLE_TOOLS.extend([FakeSearch()])

    frontend = Frontend(
        system_message="Test",
        config_path=_test_config_path(),
    )

    for info in frontend.tool_checkboxes.values():
        info["checkbox"].value = True
    frontend.backend.update_tools(
        frontend._get_selected_tools(),
    )

    # Replace the chat callback with one that simulates:
    # 1. Call the search tool
    # 2. Return LLM-style response using the result
    tool = FakeSearch()

    async def _mock_llm_callback(contents, user, instance):
        result = tool._run(query=contents)
        yield (
            f"I searched for \"{contents}\" and found: {result}\n\n"
            f"The email from alice@test.com is about a meeting."
        )

    frontend.chat_interface.callback = _mock_llm_callback

    pending: list[str] = []
    file_mgr = FileManager(registry, pending)

    layout = pn.Row(
        pn.Column(
            *frontend.sidebar_objects,
            file_mgr.panel,
            width=300,
        ),
        pn.Column(*frontend.main_objects),
        sizing_mode="stretch_both",
    )

    serve_component(page, layout)

    yield {"page": page}

    AVAILABLE_TOOLS.clear()


def test_tool_roundtrip(tool_roundtrip_app):
    """Full roundtrip: user sends message, LLM calls tool,
    response with tool result appears in chat."""
    page = tool_roundtrip_app["page"]

    # Wait for chat to be ready
    textarea = page.locator("textarea").first
    expect(textarea).to_be_visible(timeout=20000)

    # User sends a search query
    textarea.fill("meeting")
    textarea.press("Enter")

    # User message appears
    expect(
        page.locator("text=meeting").first,
    ).to_be_visible(timeout=10000)

    # Assistant response with tool result appears
    expect(
        page.locator("text=alice@test.com").first,
    ).to_be_visible(timeout=15000)

    # The response references the search result
    expect(
        page.locator("text=Meeting tomorrow").first,
    ).to_be_visible(timeout=5000)
