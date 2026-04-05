"""Create an MCP server from a BaseAgentService."""

from __future__ import annotations

import functools
import inspect
import json
import logging
import mimetypes
from pathlib import Path
from typing import Any

from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings
from mcp.types import (
    CallToolResult,
    ResourceLink,
    TextContent,
    ToolAnnotations,
)

from .base import BaseAgentService, NamedFile, ToolResult

logger = logging.getLogger(__name__)


# Normalization: any tool return value -> CallToolResult

def _file_to_resource_link(
    path: Path, name: str, base_url: str,
) -> tuple[ResourceLink, dict]:
    """Convert a file Path to a ResourceLink + metadata dict."""
    mime, _ = mimetypes.guess_type(str(path))
    mime = mime or "application/octet-stream"
    size = path.stat().st_size if path.exists() else 0
    url = f"{base_url}/files/{path.name}"
    link = ResourceLink(
        type="resource_link",
        uri=url,
        name=name,
        mimeType=mime,
        size=size,
    )
    meta = {
        "download_url": url,
        "filename": name,
        "mime_type": mime,
        "size_bytes": size,
    }
    return link, meta


def _normalize_to_tool_result(raw: Any) -> ToolResult:
    """Convert any tool return value to a ToolResult."""
    if isinstance(raw, ToolResult):
        return raw
    if isinstance(raw, (Path, NamedFile)):
        return ToolResult(files=[raw])
    if isinstance(raw, dict):
        return ToolResult(data=raw)
    if isinstance(raw, list):
        return ToolResult(data=raw)
    if isinstance(raw, str):
        # Try to detect legacy JSON file responses
        try:
            parsed = json.loads(raw)
            if isinstance(parsed, dict) and "download_url" in parsed:
                return ToolResult(data=parsed)
            if isinstance(parsed, (dict, list)):
                return ToolResult(data=parsed)
        except (json.JSONDecodeError, TypeError):
            pass
        return ToolResult(text=raw)
    if raw is None:
        return ToolResult()
    # Fallback: stringify
    return ToolResult(text=str(raw))


def _tool_result_to_call_tool_result(
    result: ToolResult, base_url: str,
) -> CallToolResult:
    """Convert a ToolResult to an MCP CallToolResult."""
    content: list = []
    structured: dict | None = None

    # Text block
    if result.text:
        content.append(TextContent(type="text", text=result.text))

    # Structured data
    if result.data is not None:
        # structuredContent accepts dict per MCP spec.
        # Wrap lists so structuredContent is always a dict.
        structured = (
            result.data if isinstance(result.data, dict)
            else {"items": result.data}
        )
        # Also add as text for LLMs that don't read structuredContent
        text = json.dumps(result.data, ensure_ascii=False, indent=2)
        content.append(TextContent(type="text", text=text))

    # Files
    for f in result.files:
        if isinstance(f, NamedFile):
            path, name = f.path, f.name
        else:
            path, name = f, f.name
        link, meta = _file_to_resource_link(path, name, base_url)
        content.append(link)
        if structured is None:
            structured = meta
        else:
            structured.update(meta)
        # Add text summary for LLMs
        content.insert(0, TextContent(
            type="text",
            text=json.dumps(meta, ensure_ascii=False),
        ))

    # Ensure at least one content block
    if not content:
        content.append(TextContent(type="text", text=""))

    return CallToolResult(
        content=content,
        structuredContent=structured,
    )


# Wrapper: tool fn -> normalized CallToolResult

def _make_normalized_wrapper(
    name: str, fn: Any, base_url_getter: Any,
) -> Any:
    """Wrap a tool function to normalize its return value to CallToolResult."""

    if inspect.iscoroutinefunction(fn):
        @functools.wraps(fn)
        async def wrapper(**kwargs):
            raw = await fn(**kwargs)
            if isinstance(raw, CallToolResult):
                return raw
            result = _normalize_to_tool_result(raw)
            return _tool_result_to_call_tool_result(
                result, base_url_getter(),
            )
    else:
        @functools.wraps(fn)
        def wrapper(**kwargs):
            raw = fn(**kwargs)
            if isinstance(raw, CallToolResult):
                return raw
            result = _normalize_to_tool_result(raw)
            return _tool_result_to_call_tool_result(
                result, base_url_getter(),
            )

    wrapper.__name__ = name
    wrapper.__qualname__ = name
    # Strip return annotation so FastMCP doesn't auto-generate
    # an output schema that conflicts with CallToolResult.
    wrapper.__annotations__.pop("return", None)
    return wrapper


def create_mcp_server(service: BaseAgentService) -> FastMCP:
    """Build a FastMCP server with all tools from the agent service."""
    security = TransportSecuritySettings(
        enable_dns_rebinding_protection=False,
    )
    server = FastMCP(
        name=service.agent_name,
        instructions=service.agent_description,
        transport_security=security,
    )

    # Enable experimental task support (in-memory store).
    has_task_tools = any(t.task_support for t in service.get_tools())
    if has_task_tools:
        try:
            server._mcp_server.experimental.enable_tasks()
            logger.info("MCP task support enabled")
        except Exception:
            logger.warning(
                "Failed to enable MCP task support", exc_info=True,
            )

    def _base_url():
        return service.base_url or "http://127.0.0.1:8000"

    for tool in service.get_tools():
        fn = _make_normalized_wrapper(tool.name, tool.fn, _base_url)
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
                prop = registered.parameters["properties"]
                if param_name in prop:
                    prop[param_name]["x-file"] = True

        # Set MCP ToolAnnotations and ToolExecution hints.
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
