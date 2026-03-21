"""Shared testing utilities for MCP integration tests."""

from __future__ import annotations

import json
from contextlib import asynccontextmanager
from typing import Any, AsyncGenerator

from mcp.client.session import ClientSession
from mcp.shared.memory import create_connected_server_and_client_session

from .base import BaseAgentService
from .mcp_server import create_mcp_server


@asynccontextmanager
async def mcp_client_for(service: BaseAgentService) -> AsyncGenerator[ClientSession, None]:
    """Create an in-memory MCP client connected to an agent service.

    Usage:
        async with mcp_client_for(my_service) as client:
            tools = await client.list_tools()
            result = await client.call_tool("tool_name", {"arg": "value"})
    """
    server = create_mcp_server(service)
    async with create_connected_server_and_client_session(
        server, raise_exceptions=True,
    ) as client:
        yield client


def parse_tool_result(result) -> Any:
    """Extract and parse the text content from a CallToolResult.

    MCP tool results are wrapped in content blocks. This extracts
    the first text block and parses it as JSON if possible.
    """
    if not result.content:
        return None
    text = result.content[0].text
    try:
        return json.loads(text)
    except (json.JSONDecodeError, TypeError):
        return text
