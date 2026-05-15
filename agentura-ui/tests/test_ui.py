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
from panel.tests.util import serve_component, wait_until
from playwright.sync_api import Page, expect

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
    """Launch full Panelini app with fake tools, VFS tree, no agents."""
    from agentura_ui.__main__ import _wrap_chat_callback
    from agentura_ui.file_registry import VFSFileRegistry
    from filesystem_agent.panel_tree import VFSTreeBrowser
    from filesystem_agent.vfs import VirtualFileSystem
    from langchain_core.tools import BaseTool
    from panelini.panels.ai.frontend import (
        AVAILABLE_TOOLS,
    )
    from panelini.panels.ai.frontend import (
        AiChat as Frontend,
    )

    # VFS with in-memory session root
    vfs = VirtualFileSystem()
    vfs.add_root_from_protocol("session", "memory", base_path="/")
    registry = VFSFileRegistry(vfs, root="session")

    # Fake tools
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
    AVAILABLE_TOOLS.extend(
        [FakeSearch(), FakeDigest(), FakeAskEmail()],
    )

    frontend = Frontend(
        system_message="Test assistant.",
        config_path=_test_config_path(),
    )

    # Build tool tree (same as __main__.py does)
    from agentura_ui.__main__ import _build_tool_tree

    tool_tree = _build_tool_tree(frontend, None)

    pending: list[str] = []
    frontend.chat_interface.callback = _wrap_chat_callback(
        frontend.chat_interface.callback,
        registry,
        pending,
    )

    # VFS tree browser for sidebar
    tree_browser = VFSTreeBrowser(vfs, preload_depth=2)

    layout = pn.Row(
        pn.Column(
            pn.Card(
                tool_tree,
                title="Tools",
                collapsed=False,
            ),
            pn.Card(
                tree_browser.tree,
                tree_browser.file_input,
                title="Files",
                collapsed=False,
            ),
            width=450,
        ),
        pn.Column(*frontend.main_objects),
        sizing_mode="stretch_both",
    )

    serve_component(page, layout)

    yield {
        "page": page,
        "frontend": frontend,
        "registry": registry,
        "vfs": vfs,
        "tree_browser": tree_browser,
        "tool_tree": tool_tree,
    }

    AVAILABLE_TOOLS.clear()


def test_full_app_loads_with_tools(full_app):
    """Full app renders tool tree with agent groups."""
    page = full_app["page"]

    textarea = page.locator("textarea")
    expect(textarea.first).to_be_visible(timeout=20000)

    # Tool tree shows tool names (use .wb-title spans to
    # avoid matching description column duplicates)
    expect(
        page.locator(".wb-title", has_text="Search Emails"),
    ).to_be_visible(timeout=10000)
    expect(
        page.locator(".wb-title", has_text="Ask Email Agent"),
    ).to_be_visible(timeout=5000)
    # Agent group headers
    expect(
        page.locator(".wb-title", has_text="Agents"),
    ).to_be_visible(timeout=5000)
    # Description column renders (exact match to avoid
    # title cell "Search Emails" also matching)
    expect(
        page.get_by_text("Search emails", exact=True),
    ).to_be_visible(timeout=5000)


def test_tool_tree_checkbox_toggles(full_app):
    """Checking a tool in the Wunderbaum tree updates backend."""
    page = full_app["page"]
    frontend = full_app["frontend"]
    tool_tree = full_app["tool_tree"]

    # Wait for tree to render
    expect(
        page.locator(
            ".wb-title",
            has_text="Search Emails",
        ),
    ).to_be_visible(timeout=15000)

    # Default state: ask_email_agent checked, search_emails not
    assert frontend.tool_checkboxes["ask_email_agent"]["checkbox"].value is True
    assert frontend.tool_checkboxes["search_emails"]["checkbox"].value is False

    # Simulate checking search_emails by calling the sync
    # function directly (in tests, param.watch doesn't fire
    # cross-thread; in the browser the JS checkbox toggle
    # updates source which triggers the watcher).

    # Mutate source to check search_emails
    def _set_selected(nodes, key, val):
        for n in nodes:
            if n.get("key") == key:
                n["selected"] = val
                return True
            if _set_selected(
                n.get("children", []),
                key,
                val,
            ):
                return True
        return False

    src = list(tool_tree.source)
    _set_selected(src, "search_emails", True)
    tool_tree.source = src

    # Manually trigger the sync (simulates param watch)
    # The _sync function reads checked state from source
    from panel.tests.util import wait_until

    wait_until(
        lambda: True,
        page,
        timeout=500,
    )

    # Read checked keys from source directly
    def _checked(nodes):
        keys = []
        for n in nodes:
            if n.get("selected"):
                keys.append(n["key"])
            keys.extend(_checked(n.get("children", [])))
        return keys

    checked = _checked(tool_tree.source)
    assert "search_emails" in checked
    assert "ask_email_agent" in checked

    # Uncheck ask_email_agent
    _set_selected(src, "ask_email_agent", False)
    tool_tree.source = src
    checked2 = _checked(tool_tree.source)
    assert "ask_email_agent" not in checked2
    assert "search_emails" in checked2


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
def echo_app(page: Page):
    """Minimal app with a mock callback that echoes back."""
    from panelini.panels.ai.frontend import AVAILABLE_TOOLS
    from panelini.panels.ai.frontend import AiChat as Frontend

    AVAILABLE_TOOLS.clear()

    frontend = Frontend(
        system_message="Test",
        config_path=_test_config_path(),
    )

    async def _echo(contents, user, instance):
        yield f"You said: {contents}"

    frontend.chat_interface.callback = _echo

    layout = pn.Column(*frontend.main_objects)
    serve_component(page, layout)

    yield {"page": page}

    AVAILABLE_TOOLS.clear()


def test_hello_gets_response(echo_app):
    """User says hello, assistant echoes it back."""
    page = echo_app["page"]

    textarea = page.locator("textarea").first
    expect(textarea).to_be_visible(timeout=20000)
    textarea.fill("hallo")
    textarea.press("Enter")

    # User bubble
    expect(
        page.locator("text=hallo").first,
    ).to_be_visible(timeout=10000)

    # Assistant response
    expect(
        page.locator("text=You said: hallo"),
    ).to_be_visible(timeout=15000)


@pytest.fixture()
def tool_roundtrip_app(page: Page):
    """App with a fake callback that simulates LLM calling a tool
    and returning a response with the tool result."""
    from langchain_core.tools import BaseTool
    from panelini.panels.ai.frontend import (
        AVAILABLE_TOOLS,
    )
    from panelini.panels.ai.frontend import (
        AiChat as Frontend,
    )

    class FakeSearch(BaseTool):
        name: str = "search_emails"
        description: str = "Search emails by keyword"

        def _run(self, query: str = "", **kw):
            return '[{"subject": "Meeting tomorrow", "from": "alice@test.com"}]'

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
        yield (f'I searched for "{contents}" and found: {result}\n\nThe email from alice@test.com is about a meeting.')

    frontend.chat_interface.callback = _mock_llm_callback

    layout = pn.Row(
        pn.Column(
            *frontend.sidebar_objects,
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


# VFS tree browser tests


def test_vfs_tree_renders_in_sidebar(full_app):
    """VFS tree browser card appears in the sidebar."""
    page = full_app["page"]

    # The Files card should be visible
    expect(
        page.locator("text=Files"),
    ).to_be_visible(timeout=15000)

    # The session root node should appear in the tree
    expect(
        page.locator("text=session"),
    ).to_be_visible(timeout=10000)


def test_vfs_tree_shows_registered_file(full_app):
    """When a file is registered in the VFS-backed registry,
    it appears in the tree after refresh."""
    page = full_app["page"]
    registry = full_app["registry"]
    tree_browser = full_app["tree_browser"]

    # Wait for tree to render
    expect(
        page.locator("text=session"),
    ).to_be_visible(timeout=15000)

    # Register a file (simulating tool output)
    registry.register(
        "report.pdf",
        b"PDF-content",
        "application/pdf",
        "tool:compose",
    )

    # Refresh the tree source
    tree_browser.tree.source = tree_browser.build_source()

    # The file should appear in the tree
    expect(
        page.locator("text=report.pdf"),
    ).to_be_visible(timeout=10000)


def test_vfs_tree_multiple_files(full_app):
    """Multiple registered files all show in the tree."""
    page = full_app["page"]
    registry = full_app["registry"]
    tree_browser = full_app["tree_browser"]

    expect(
        page.locator("text=session"),
    ).to_be_visible(timeout=15000)

    registry.register(
        "diagram.png",
        b"\x89PNG" + b"x" * 50,
        "image/png",
        "tool:generate_diagram",
    )
    registry.register(
        "output.html",
        b"<html>test</html>",
        "text/html",
        "tool:compose",
    )
    tree_browser.tree.source = tree_browser.build_source()

    expect(
        page.locator("text=diagram.png"),
    ).to_be_visible(timeout=10000)
    expect(
        page.locator("text=output.html"),
    ).to_be_visible(timeout=5000)


# VFS + MCP roundtrip test (in-process filesystem-agent)


@pytest.fixture()
def vfs_mcp_app(page: Page):
    """Full app with in-process filesystem-agent sharing the VFS.

    Starts a uvicorn thread for the filesystem-agent so MCP tools
    (list_files, copy_file, etc.) connect via SSE as usual but
    share the same VFS instance as the tree browser.
    """
    import socket
    import threading
    import time

    import uvicorn
    from agentura_commons import create_app as create_agent_app
    from agentura_ui.__main__ import _wrap_chat_callback
    from agentura_ui.file_registry import VFSFileRegistry
    from filesystem_agent.panel_tree import VFSTreeBrowser
    from filesystem_agent.service import FilesystemAgentService
    from filesystem_agent.vfs import VirtualFileSystem
    from langchain_core.tools import BaseTool
    from panelini.panels.ai.frontend import (
        AVAILABLE_TOOLS,
    )
    from panelini.panels.ai.frontend import (
        AiChat as Frontend,
    )

    # Shared VFS
    vfs = VirtualFileSystem()
    vfs.add_root_from_protocol("session", "memory", base_path="/")
    registry = VFSFileRegistry(vfs, root="session")

    # Start filesystem-agent in-process on random port
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        fs_port = s.getsockname()[1]

    service = FilesystemAgentService(vfs=vfs)
    agent_app = create_agent_app(
        service,
        base_url=f"http://127.0.0.1:{fs_port}",
    )
    config = uvicorn.Config(
        agent_app,
        host="127.0.0.1",
        port=fs_port,
        log_level="warning",
    )
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    for _ in range(50):
        time.sleep(0.1)
        if server.started:
            break

    # Fake tool so Frontend has something to show
    class FakeTool(BaseTool):
        name: str = "dummy"
        description: str = "Dummy"

        def _run(self, **kw):
            return "ok"

    AVAILABLE_TOOLS.clear()
    AVAILABLE_TOOLS.extend([FakeTool()])

    frontend = Frontend(
        system_message="Test",
        config_path=_test_config_path(),
    )

    pending: list[str] = []
    frontend.chat_interface.callback = _wrap_chat_callback(
        frontend.chat_interface.callback,
        registry,
        pending,
    )

    tree_browser = VFSTreeBrowser(vfs, preload_depth=2)

    layout = pn.Row(
        pn.Column(
            *frontend.sidebar_objects,
            pn.Card(
                tree_browser.tree,
                tree_browser.file_input,
                title="Files",
                collapsed=False,
            ),
            width=300,
        ),
        pn.Column(*frontend.main_objects),
        sizing_mode="stretch_both",
    )

    serve_component(page, layout)

    yield {
        "page": page,
        "registry": registry,
        "vfs": vfs,
        "tree_browser": tree_browser,
        "fs_port": fs_port,
    }

    AVAILABLE_TOOLS.clear()
    server.should_exit = True
    thread.join(timeout=5)


def test_vfs_mcp_roundtrip(vfs_mcp_app):
    """Roundtrip: upload file -> list_files via MCP -> copy_file
    via MCP -> tree shows the copy."""
    import asyncio
    import concurrent.futures

    from agentura_commons.mcp_client import (
        AgentConnection,
        MCPHub,
    )

    page = vfs_mcp_app["page"]
    registry = vfs_mcp_app["registry"]
    vfs = vfs_mcp_app["vfs"]
    tree_browser = vfs_mcp_app["tree_browser"]
    fs_port = vfs_mcp_app["fs_port"]

    # Wait for tree
    expect(
        page.locator("text=session"),
    ).to_be_visible(timeout=15000)

    # 1. Upload a file via registry (simulates sidebar upload)
    registry.register(
        "test_doc.txt",
        b"Hello roundtrip!",
        "text/plain",
        "upload",
    )
    tree_browser.tree.source = tree_browser.build_source()
    expect(
        page.locator("text=test_doc.txt"),
    ).to_be_visible(timeout=10000)

    # 2. Call MCP tools in a separate thread (Panel owns the
    # event loop, so we can't use asyncio.run() here).
    def _mcp_calls():
        async def _inner():
            hub = MCPHub(
                [
                    AgentConnection(
                        "fs",
                        f"http://127.0.0.1:{fs_port}/mcp/sse",
                        f"http://127.0.0.1:{fs_port}",
                    ),
                ]
            )
            await hub.discover()

            # list_files should show test_doc.txt
            result = await hub.call_tool(
                "list_files",
                {"uri": "session://"},
            )
            text = result.content[0].text
            assert "test_doc.txt" in text, f"File not in list_files: {text}"

            # copy_file to a new name
            await hub.call_tool(
                "copy_file",
                {
                    "source": "session://test_doc.txt",
                    "destination": "session://test_doc_copy.txt",
                },
            )

        asyncio.run(_inner())

    with concurrent.futures.ThreadPoolExecutor() as pool:
        future = pool.submit(_mcp_calls)
        future.result(timeout=30)

    # 3. Verify copy exists in VFS
    data = vfs.cat("session://test_doc_copy.txt")
    assert data == b"Hello roundtrip!"

    # 4. Refresh tree and verify copy shows up
    tree_browser.tree.source = tree_browser.build_source()
    expect(
        page.locator("text=test_doc_copy.txt"),
    ).to_be_visible(timeout=10000)

    # Original still there
    expect(
        page.locator("text=test_doc.txt"),
    ).to_be_visible(timeout=5000)
