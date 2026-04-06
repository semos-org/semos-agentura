"""Shared testing utilities for MCP integration tests."""

from __future__ import annotations

import json
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from typing import Any

from mcp.client.session import ClientSession
from mcp.shared.memory import create_connected_server_and_client_session

from .base import BaseAgentService
from .mcp_server import create_mcp_server


@asynccontextmanager
async def mcp_client_for(service: BaseAgentService) -> AsyncGenerator[ClientSession]:
    """Create an in-memory MCP client connected to an agent service.

    Usage:
        async with mcp_client_for(my_service) as client:
            tools = await client.list_tools()
            result = await client.call_tool("tool_name", {"arg": "value"})
    """
    server = create_mcp_server(service)
    async with create_connected_server_and_client_session(
        server,
        raise_exceptions=True,
    ) as client:
        yield client


def parse_tool_result(result) -> Any:
    """Extract and parse the content from a CallToolResult.

    Prefers structuredContent (dict) if available.
    Unwraps {"items": [...]} back to a plain list (list results
    get wrapped in a dict for MCP structuredContent compatibility).
    Falls back to parsing the first TextContent block as JSON.
    """
    sc = getattr(result, "structuredContent", None)
    if sc is not None:
        # Unwrap list wrapper
        if isinstance(sc, dict) and list(sc.keys()) == ["items"]:
            return sc["items"]
        return sc
    if not result.content:
        return None
    text = result.content[0].text
    try:
        return json.loads(text)
    except (json.JSONDecodeError, TypeError):
        return text
