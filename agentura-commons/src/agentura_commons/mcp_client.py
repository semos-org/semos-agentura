"""Multi-agent MCP SSE connection manager.

Extracted from agentura-ui/mcp_hub.py (protocol-level, no UI deps).
"""

from __future__ import annotations

import logging
from contextlib import AsyncExitStack
from dataclasses import dataclass, field

from mcp.client.session import ClientSession
from mcp.client.sse import sse_client
from mcp.types import CallToolResult, Tool

logger = logging.getLogger(__name__)


@dataclass
class AgentConnection:
    """A single MCP agent endpoint."""

    name: str
    url: str       # SSE endpoint (e.g. http://localhost:8002/mcp/sse)
    base_url: str  # For file downloads (e.g. http://localhost:8002)
    session: ClientSession | None = field(default=None, repr=False)
    tools: list[Tool] = field(default_factory=list)


class MCPHub:
    """Manages persistent SSE connections to multiple MCP agents.

    Supports a two-phase lifecycle:
    1. discover() - connect, list tools, disconnect
    2. ensure_connected() - reconnect lazily on first tool call
    """

    def __init__(self, agents: list[AgentConnection]) -> None:
        self._agents = {a.name: a for a in agents}
        self._tool_to_agent: dict[str, str] = {}
        self._exit_stack: AsyncExitStack | None = None

    @property
    def agents(self) -> dict[str, AgentConnection]:
        return self._agents

    async def connect_all(self) -> None:
        """Open SSE connections, initialize sessions, list tools."""
        self._exit_stack = AsyncExitStack()
        for agent in self._agents.values():
            logger.info(
                "Connecting to %s at %s", agent.name, agent.url,
            )
            streams = await self._exit_stack.enter_async_context(
                sse_client(agent.url),
            )
            session = await self._exit_stack.enter_async_context(
                ClientSession(*streams),
            )
            await session.initialize()
            result = await session.list_tools()
            agent.session = session
            agent.tools = result.tools
            for tool in result.tools:
                self._tool_to_agent[tool.name] = agent.name
            logger.info(
                "Connected to %s: %d tools",
                agent.name, len(result.tools),
            )

    async def disconnect_all(self) -> None:
        """Close all connections (keeps tool metadata)."""
        for agent in self._agents.values():
            agent.session = None
        if self._exit_stack:
            await self._exit_stack.aclose()
            self._exit_stack = None

    async def ensure_connected(self) -> None:
        """Reconnect if not currently connected."""
        any_disconnected = any(
            a.session is None for a in self._agents.values()
        )
        if any_disconnected:
            logger.info("Reconnecting to MCP agents...")
            await self.connect_all()

    async def discover(self) -> list[Tool]:
        """Connect, list tools, disconnect. Returns tool metadata.

        Tool metadata (names, schemas) is preserved after disconnect.
        """
        await self.connect_all()
        tools = self.all_tools()
        await self.disconnect_all()
        return tools

    def all_tools(self) -> list[Tool]:
        """Return all tools from all connected agents."""
        tools: list[Tool] = []
        for agent in self._agents.values():
            tools.extend(agent.tools)
        return tools

    def tool_schema(self, tool_name: str) -> Tool | None:
        """Get the Tool schema for a tool by name."""
        for agent in self._agents.values():
            for tool in agent.tools:
                if tool.name == tool_name:
                    return tool
        return None

    def agent_for_tool(self, tool_name: str) -> AgentConnection:
        """Look up which agent owns a tool."""
        agent_name = self._tool_to_agent[tool_name]
        return self._agents[agent_name]

    async def call_tool(
        self, tool_name: str, arguments: dict,
    ) -> CallToolResult:
        """Route a tool call to the correct agent."""
        await self.ensure_connected()
        agent = self.agent_for_tool(tool_name)
        assert agent.session is not None
        return await agent.session.call_tool(
            tool_name, arguments=arguments,
        )
