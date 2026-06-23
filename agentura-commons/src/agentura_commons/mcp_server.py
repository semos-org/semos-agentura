# TODO: Replace _make_normalized_wrapper with langchain_mcp_adapters.to_fastmcp()
# once the recursion bug with AgentTool extra fields is resolved.
"""Create an MCP server from a BaseAgentService."""

from __future__ import annotations

import functools
import inspect
import json
import logging
import mimetypes
import uuid
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
    path: Path,
    name: str,
    base_url: str,
    output_dir: Path | None = None,
) -> tuple[ResourceLink, dict]:
    """Convert a file Path to a ResourceLink + metadata dict."""
    from urllib.parse import quote

    mime, _ = mimetypes.guess_type(str(path))
    mime = mime or "application/octet-stream"
    size = path.stat().st_size if path.exists() else 0
    # Compute path relative to output_dir for subdirectory support
    if output_dir and path.is_absolute():
        try:
            rel = path.relative_to(output_dir)
        except ValueError:
            rel = Path(path.name)
    else:
        rel = Path(path.name)
    url_path = "/".join(quote(part) for part in rel.parts)
    url = f"{base_url}/files/{url_path}"
    link = ResourceLink(
        type="resource_link",
        uri=url,
        name=name,
        mimeType=mime,
        size=size,
    )
    # structuredContent metadata (no URLs - those are in ResourceLink)
    meta = {
        "filename": name,
        "mime_type": mime,
        "size_bytes": size,
    }
    return link, meta


def _is_file_like(obj: Any) -> bool:
    """Check if obj is a file-like object (has read method)."""
    return hasattr(obj, "read") and callable(obj.read)


def _materialize_file(
    obj: Any,
    output_dir: Path | None,
) -> Path:
    """Write a file-like object to output_dir and return the Path."""
    data = obj.read()
    if isinstance(data, str):
        data = data.encode("utf-8")
    name = getattr(obj, "name", None)
    if name:
        name = Path(name).name
    else:
        name = f"_file_{uuid.uuid4().hex[:8]}.bin"
    safe = f"{uuid.uuid4().hex[:8]}_{name}"
    dest = (output_dir or Path(".")) / safe
    dest.write_bytes(data)
    return dest


def _normalize_to_tool_result(
    raw: Any,
    output_dir: Path | None = None,
) -> ToolResult:
    """Convert any tool return value to a ToolResult."""
    if isinstance(raw, ToolResult):
        return raw
    if isinstance(raw, (Path, NamedFile)):
        return ToolResult(files=[raw])
    if _is_file_like(raw):
        return ToolResult(files=[_materialize_file(raw, output_dir)])
    if isinstance(raw, list):
        # Check if it's a list of files (Path, NamedFile, file-like)
        if raw and all(isinstance(x, (Path, NamedFile)) or _is_file_like(x) for x in raw):
            files = []
            for x in raw:
                if _is_file_like(x):
                    files.append(_materialize_file(x, output_dir))
                else:
                    files.append(x)
            return ToolResult(files=files)
        return ToolResult(data=raw)
    if isinstance(raw, dict):
        return ToolResult(data=raw)
    if isinstance(raw, str):
        try:
            parsed = json.loads(raw)
            if isinstance(parsed, (dict, list)):
                return ToolResult(data=parsed)
        except (json.JSONDecodeError, TypeError):
            pass
        return ToolResult(text=raw)
    if raw is None:
        return ToolResult()
    return ToolResult(text=str(raw))


def _tool_result_to_call_tool_result(
    result: ToolResult,
    base_url: str,
    output_dir: Path | None = None,
) -> CallToolResult:
    """Convert a ToolResult to an MCP CallToolResult."""
    content: list = []
    structured: dict | None = None

    # Detect error results: {"error": ...} dicts -> isError=True
    if isinstance(result.data, dict) and "error" in result.data:
        err = result.data["error"]
        if isinstance(err, list):
            text = "\n".join(str(e) for e in err)
        else:
            text = str(err)
        content.append(TextContent(type="text", text=text))
        return CallToolResult(content=content, isError=True)

    # Text block
    if result.text:
        content.append(TextContent(type="text", text=result.text))

    # Structured data
    if result.data is not None:
        # structuredContent accepts dict per MCP spec.
        # Wrap lists so structuredContent is always a dict.
        structured = result.data if isinstance(result.data, dict) else {"items": result.data}
        # Also add as text for LLMs that don't read structuredContent
        text = json.dumps(result.data, ensure_ascii=False, indent=2)
        content.append(TextContent(type="text", text=text))

    # Files: each gets a ResourceLink in content + metadata in structuredContent
    file_metas = []
    for f in result.files:
        if isinstance(f, NamedFile):
            path, name = f.path, f.name
        else:
            path, name = f, f.name
        link, meta = _file_to_resource_link(path, name, base_url, output_dir)
        content.append(link)
        file_metas.append(meta)

    if file_metas:
        # Add file summaries as text for LLMs
        summary = ", ".join(f"{m['filename']} ({m['size_bytes']} bytes)" for m in file_metas)
        content.insert(0, TextContent(type="text", text=f"Produced: {summary}"))
        # Merge into structuredContent
        if structured is None:
            structured = {}
        if len(file_metas) == 1:
            structured.update(file_metas[0])
        else:
            structured["files"] = file_metas

    # Ensure at least one content block
    if not content:
        content.append(TextContent(type="text", text=""))

    return CallToolResult(
        content=content,
        structuredContent=structured,
    )


# Wrapper: tool fn -> normalized CallToolResult


def _make_normalized_wrapper(
    name: str,
    fn: Any,
    service: BaseAgentService,
    args_schema: Any = None,
) -> Any:
    """Wrap a tool function to normalize its return value to CallToolResult."""

    def _base_url():
        return service.base_url or "http://127.0.0.1:8000"

    def _validate(kwargs: dict) -> None:
        # Enforce the tool's Pydantic schema (required fields, types) before
        # dispatch. Raises pydantic.ValidationError, which FastMCP surfaces as
        # a tool error - stops e.g. a write_file call that omits content from
        # silently succeeding with an empty body.
        if args_schema is not None:
            args_schema.model_validate(kwargs)

    if inspect.iscoroutinefunction(fn):

        @functools.wraps(fn)
        async def wrapper(**kwargs):
            _validate(kwargs)
            raw = await fn(**kwargs)
            if isinstance(raw, CallToolResult):
                return raw
            result = _normalize_to_tool_result(raw, service.output_dir)
            return _tool_result_to_call_tool_result(
                result,
                _base_url(),
                service.output_dir,
            )
    else:

        @functools.wraps(fn)
        def wrapper(**kwargs):
            _validate(kwargs)
            raw = fn(**kwargs)
            if isinstance(raw, CallToolResult):
                return raw
            result = _normalize_to_tool_result(raw, service.output_dir)
            return _tool_result_to_call_tool_result(
                result,
                _base_url(),
                service.output_dir,
            )

    wrapper.__name__ = name
    wrapper.__qualname__ = name
    # Strip return annotation so FastMCP doesn't auto-generate
    # an output schema that conflicts with CallToolResult.
    wrapper.__annotations__.pop("return", None)

    # For AgentTool._arun (which has **kwargs), copy the args_schema
    # field signatures onto the wrapper so FastMCP generates the
    # correct parameter schema instead of a single "kwargs" field.
    if args_schema is not None:
        sig_params = [inspect.Parameter("self", inspect.Parameter.POSITIONAL_OR_KEYWORD)]
        annotations = {}
        for field_name, field_info in args_schema.model_fields.items():
            if field_info.is_required():
                default = inspect.Parameter.empty
            else:
                default = field_info.default
            sig_params.append(inspect.Parameter(field_name, inspect.Parameter.KEYWORD_ONLY, default=default))
            if field_info.annotation is not None:
                annotations[field_name] = field_info.annotation
        # Remove 'self' - FastMCP doesn't expect it
        sig_params = sig_params[1:]
        wrapper.__signature__ = inspect.Signature(sig_params)
        wrapper.__annotations__ = annotations

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
                "Failed to enable MCP task support",
                exc_info=True,
            )

    for tool in service.get_tools():
        file_params = tool.resolved_file_params
        fn = _make_normalized_wrapper(
            tool.name,
            tool._arun,
            service,
            args_schema=tool.args_schema,
        )
        server.add_tool(fn=fn, name=tool.name, description=tool.description)

        registered = server._tool_manager._tools.get(tool.name)
        if not registered:
            continue

        # Replace FastMCP's auto-inferred schema with the full
        # Pydantic-generated schema (has proper types, descriptions,
        # nested models like EmbedItem, $defs, etc.)
        if tool.args_schema:
            pydantic_schema = tool.get_input_schema()
            if pydantic_schema.get("properties"):
                registered = registered.model_copy(update={"parameters": pydantic_schema})
                server._tool_manager._tools[tool.name] = registered

        # Inject x-file annotations for file params.
        if file_params and "properties" in registered.parameters:
            for param_name in file_params:
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
