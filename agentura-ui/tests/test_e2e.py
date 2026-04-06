"""End-to-end tests: MCP tools, A2A tools, A2A delegates.

All agents are auto-started on random ports with mocked backends
(no Outlook COM, no pandoc, no OCR). No manual startup needed.

Run with: uv run pytest agentura-ui/tests/test_e2e.py -v
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

from agentura_ui.file_registry import FileRegistry

# Import shared fixtures from agentura-commons/tests/conftest.py
_commons_conftest = (
    Path(__file__).resolve().parent.parent.parent
    / "agentura-commons" / "tests" / "conftest.py"
)
_spec = importlib.util.spec_from_file_location(
    "commons_conftest", _commons_conftest,
)
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)
free_port = _mod.free_port
start_agent = _mod.start_agent


# Fixtures: auto-start agents on random ports

@pytest.fixture(scope="module")
def email_agent():
    port = free_port()
    base = f"http://127.0.0.1:{port}"
    server, thread = start_agent("email_agent", port)
    yield {"port": port, "base_url": base, "sse": f"{base}/mcp/sse"}
    server.should_exit = True
    thread.join(timeout=5)


@pytest.fixture(scope="module")
def doc_agent():
    port = free_port()
    base = f"http://127.0.0.1:{port}"
    server, thread = start_agent("document_agent", port)
    yield {"port": port, "base_url": base, "sse": f"{base}/mcp/sse"}
    server.should_exit = True
    thread.join(timeout=5)


# Helpers

async def _make_hub(agent_info):
    from agentura_ui.mcp_hub import AgentConnection, MCPHub
    hub = MCPHub([AgentConnection(
        "agent", agent_info["sse"], agent_info["base_url"],
    )])
    await hub.discover()
    return hub


async def _discover_a2a(base_url):
    from agentura_ui.a2a_client import discover_agents
    agents = await discover_agents([base_url])
    assert len(agents) == 1
    return agents[0]


# MCP Tool tests

class TestMCPTool:

    @pytest.mark.asyncio
    async def test_search_emails(self, email_agent):
        from agentura_ui.mcp_tools import _make_mcp_tool_class

        registry = FileRegistry()
        hub = await _make_hub(email_agent)
        assert len(hub.all_tools()) >= 9

        mcp_tool = next(
            t for t in hub.all_tools()
            if t.name == "search_emails"
        )
        wrapper = _make_mcp_tool_class(mcp_tool, hub, registry)
        result = await wrapper._arun(query="test", limit=5)
        assert isinstance(result, str)
        assert len(result) > 0

    @pytest.mark.asyncio
    async def test_compose_document_produces_file(self, doc_agent):
        from agentura_ui.mcp_tools import (
            _make_mcp_tool_class, drain_produced_files,
        )

        registry = FileRegistry()
        hub = await _make_hub(doc_agent)

        mcp_tool = next(
            t for t in hub.all_tools()
            if t.name == "compose_document"
        )
        wrapper = _make_mcp_tool_class(mcp_tool, hub, registry)
        result = await wrapper._arun(
            source="# Test\n\nHello.", format="html",
        )
        assert isinstance(result, str)

        produced = drain_produced_files()
        assert len(produced) >= 1
        assert produced[0].filename.endswith(".html")
        assert registry.get(produced[0].filename) is not None

    @pytest.mark.asyncio
    async def test_digest_with_uploaded_file(self, doc_agent):
        from agentura_ui.mcp_tools import _make_mcp_tool_class

        registry = FileRegistry()
        hub = await _make_hub(doc_agent)

        # Register a file as if user uploaded it
        registry.register(
            "test.pdf", b"%PDF-1.4 test content",
            "application/pdf", "upload",
        )

        mcp_tool = next(
            t for t in hub.all_tools()
            if t.name == "digest_document"
        )
        wrapper = _make_mcp_tool_class(mcp_tool, hub, registry)
        result = await wrapper._arun(source="test.pdf")
        assert isinstance(result, str)
        assert len(result) > 0


# A2A Tool tests (explicit tool call via DataPart)

class TestA2ATool:

    @pytest.mark.asyncio
    async def test_search_emails_via_a2a(self, email_agent):
        from agentura_ui.a2a_client import send_tool_call

        agent = await _discover_a2a(email_agent["base_url"])
        text, files = await send_tool_call(
            agent, "search_emails",
            {"query": "test", "limit": 5},
        )
        assert isinstance(text, str)
        assert len(text) > 10, f"Expected results, got: {text!r}"

    @pytest.mark.asyncio
    async def test_compose_via_a2a_returns_file(self, doc_agent):
        from agentura_ui.a2a_client import send_tool_call
        import httpx

        agent = await _discover_a2a(doc_agent["base_url"])
        text, files = await send_tool_call(
            agent, "compose_document",
            {"source": "# A2A Test\n\nContent.", "format": "html"},
        )
        assert len(files) >= 1, (
            f"Expected file artifact. text={text!r} files={files}"
        )
        assert files[0].url.startswith("http")

        # Verify file is downloadable
        async with httpx.AsyncClient() as client:
            resp = await client.get(files[0].url, timeout=10)
            resp.raise_for_status()
            assert len(resp.content) > 50


# A2A Delegate tests (natural language via ask_* tools)

class TestA2ADelegate:

    @pytest.mark.asyncio
    async def test_delegate_email_search(self, email_agent):
        from agentura_ui.a2a_tools import _make_a2a_delegate_tool

        registry = FileRegistry()
        agent = await _discover_a2a(email_agent["base_url"])
        delegate = _make_a2a_delegate_tool(agent, registry)
        assert delegate.name.startswith("ask_")

        result = await delegate._arun(
            message="search for emails about test",
        )
        assert isinstance(result, str)
        assert len(result) > 5

    @pytest.mark.asyncio
    async def test_delegate_compose_produces_file(self, doc_agent):
        from agentura_ui.a2a_tools import _make_a2a_delegate_tool
        from agentura_ui.mcp_tools import drain_produced_files

        registry = FileRegistry()
        agent = await _discover_a2a(doc_agent["base_url"])
        delegate = _make_a2a_delegate_tool(agent, registry)

        result = await delegate._arun(
            message="compose an HTML document from: # Hello\n\nWorld",
        )
        assert isinstance(result, str)

    @pytest.mark.asyncio
    async def test_delegate_with_file_attachment(self, doc_agent):
        """Delegate sends a registered file to the agent."""
        from agentura_ui.a2a_tools import _make_a2a_delegate_tool

        registry = FileRegistry()
        registry.register(
            "upload.pdf",
            b"%PDF-1.4 delegate transfer test",
            "application/pdf", "upload",
        )

        agent = await _discover_a2a(doc_agent["base_url"])
        delegate = _make_a2a_delegate_tool(agent, registry)
        result = await delegate._arun(
            message="digest the file upload.pdf",
        )
        assert isinstance(result, str)
        assert len(result) > 5


# Discovery test (MCP + A2A from same auto-started agents)

class TestDiscovery:

    @pytest.mark.asyncio
    async def test_mcp_and_a2a_from_same_agent(self, email_agent):
        """Same agent serves both MCP and A2A."""
        from agentura_ui.a2a_tools import create_a2a_delegates

        hub = await _make_hub(email_agent)
        mcp_names = {t.name for t in hub.all_tools()}
        assert "search_emails" in mcp_names

        agent = await _discover_a2a(email_agent["base_url"])
        assert agent.name  # has a name from Agent Card
        assert len(agent.skills) >= 1

        delegates = create_a2a_delegates([agent], FileRegistry())
        assert len(delegates) == 1
        assert delegates[0].name.startswith("ask_")

    @pytest.mark.asyncio
    async def test_full_tool_registration(
        self, email_agent, doc_agent,
    ):
        """Mirrors main() startup: MCP tools + A2A delegates."""
        from agentura_ui.a2a_client import discover_agents
        from agentura_ui.a2a_tools import create_a2a_delegates
        from agentura_ui.mcp_hub import AgentConnection, MCPHub
        from agentura_ui.mcp_tools import create_mcp_tools

        registry = FileRegistry()
        hub = MCPHub([
            AgentConnection(
                "email", email_agent["sse"],
                email_agent["base_url"],
            ),
            AgentConnection(
                "doc", doc_agent["sse"],
                doc_agent["base_url"],
            ),
        ])
        await hub.discover()

        mcp_tools = create_mcp_tools(hub, registry)
        assert len(mcp_tools) >= 14  # 9 email + 5 doc

        a2a_agents = await discover_agents([
            email_agent["base_url"],
            doc_agent["base_url"],
        ])
        delegates = create_a2a_delegates(a2a_agents, registry)
        assert len(delegates) == 2

        all_tools = mcp_tools + delegates
        names = {t.name for t in all_tools}
        assert "search_emails" in names
        assert "compose_document" in names
        assert "ask_email_agent" in names or any(
            n.startswith("ask_") for n in names
        )
