"""Create an MCP server from a BaseAgentService."""

from __future__ import annotations

import functools
import inspect
from typing import Any

from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings

from .base import BaseAgentService


def _make_named_wrapper(name: str, fn: Any) -> Any:
    """Wrap a function/partial so it has a proper __name__ for MCP introspection.

    MCP's FastMCP uses function introspection to generate JSON Schema.
    functools.partial doesn't have __name__, so we wrap it.
    """
    if hasattr(fn, "__name__"):
        return fn

    # For async functions
    if inspect.iscoroutinefunction(fn):
        @functools.wraps(fn)
        async def wrapper(**kwargs):
            return await fn(**kwargs)
    else:
        @functools.wraps(fn)
        def wrapper(**kwargs):
            return fn(**kwargs)

    wrapper.__name__ = name
    wrapper.__qualname__ = name
    return wrapper


def create_mcp_server(service: BaseAgentService) -> FastMCP:
    """Build a FastMCP server with all tools from the agent service."""
    # Disable DNS rebinding protection so Docker containers
    # (via host.docker.internal) and other local clients can connect.
    # Re-enable with proper allowed_hosts/allowed_origins in production.
    security = TransportSecuritySettings(
        enable_dns_rebinding_protection=False,
    )
    server = FastMCP(
        name=service.agent_name,
        instructions=service.agent_description,
        transport_security=security,
    )

    for tool in service.get_tools():
        fn = _make_named_wrapper(tool.name, tool.fn)
        server.add_tool(
            fn=fn,
            name=tool.name,
            description=tool.description,
        )

        # Inject x-file annotations into the JSON schema for file parameters.
        # This tells middleware which params accept file references.
        if tool.file_params:
            registered = server._tool_manager._tools.get(tool.name)
            if registered and "properties" in registered.parameters:
                for param_name in tool.file_params:
                    if param_name in registered.parameters["properties"]:
                        registered.parameters["properties"][param_name]["x-file"] = True

    return server
