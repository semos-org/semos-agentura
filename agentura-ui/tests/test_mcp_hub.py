"""Tests for mcp_hub.py - MCPHub connection management."""

from __future__ import annotations

from agentura_ui.mcp_hub import AgentConnection, MCPHub


class TestAgentConnection:
    def test_defaults(self):
        ac = AgentConnection("a", "http://x/mcp/sse", "http://x")
        assert ac.session is None
        assert ac.tools == []


class TestMCPHubRouting:
    """Tests that don't require live SSE connections."""

    def test_all_tools_empty_initially(self, two_agents):
        hub = MCPHub(two_agents)
        assert hub.all_tools() == []

    def test_agent_for_tool_after_manual_setup(self, two_agents):
        from mcp.types import Tool as MCPTool

        hub = MCPHub(two_agents)
        # Simulate discovered tools
        t1 = MCPTool(
            name="search_emails", description="",
            inputSchema={},
        )
        t2 = MCPTool(
            name="digest_document", description="",
            inputSchema={},
        )
        hub._agents["email-agent"].tools = [t1]
        hub._agents["document-agent"].tools = [t2]
        hub._tool_to_agent["search_emails"] = "email-agent"
        hub._tool_to_agent["digest_document"] = "document-agent"

        assert hub.agent_for_tool("search_emails").name == (
            "email-agent"
        )
        assert hub.agent_for_tool("digest_document").name == (
            "document-agent"
        )

    def test_all_tools_merged(self, two_agents):
        from mcp.types import Tool as MCPTool

        hub = MCPHub(two_agents)
        t1 = MCPTool(name="a", description="", inputSchema={})
        t2 = MCPTool(name="b", description="", inputSchema={})
        hub._agents["email-agent"].tools = [t1]
        hub._agents["document-agent"].tools = [t2]

        all_tools = hub.all_tools()
        assert len(all_tools) == 2
        assert {t.name for t in all_tools} == {"a", "b"}

    def test_agent_for_tool_unknown_raises(self, two_agents):
        import pytest

        hub = MCPHub(two_agents)
        with pytest.raises(KeyError):
            hub.agent_for_tool("nonexistent_tool")
