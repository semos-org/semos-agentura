"""Create an MCP server from a BaseAgentService."""

from __future__ import annotations

import functools
import inspect
from typing import Any

from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings
from mcp.types import ToolAnnotations

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

        registered = server._tool_manager._tools.get(tool.name)
        if not registered:
            continue

        # Inject x-file annotations into JSON schema for file params.
        if tool.file_params and "properties" in registered.parameters:
            for param_name in tool.file_params:
                if param_name in registered.parameters["properties"]:
                    registered.parameters["properties"][param_name]["x-file"] = True

        # Set MCP ToolAnnotations and ToolExecution hints.
        # Pydantic models may be frozen, so use model_copy to update.
        updates: dict = {}
        if tool.read_only or tool.destructive or tool.idempotent:
            updates["annotations"] = ToolAnnotations(
                readOnlyHint=tool.read_only or None,
                destructiveHint=tool.destructive or None,
                idempotentHint=tool.idempotent or None,
            )
        if tool.task_support:
            from mcp.types import ToolExecution
            updates["execution"] = ToolExecution(
                taskSupport=tool.task_support,
            )
        if updates:
            patched = registered.model_copy(update=updates)
            server._tool_manager._tools[tool.name] = patched

    return server
