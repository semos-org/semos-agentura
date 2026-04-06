"""LangChain BaseTool wrappers for MCP tools.

Each MCP tool is wrapped as a LangChain BaseTool so panelini's AiBackend
tool execution loop can call it. File handling middleware (pre/post) from
file_registry.py is applied inside each tool's _arun method.
"""

from __future__ import annotations

import logging
from typing import Any

from langchain_core.tools import BaseTool
from mcp.types import Tool as MCPTool
from pydantic import BaseModel, Field, create_model

from .file_registry import (
    FileEntry,
    FileRegistry,
    post_process_tool_result,
    pre_process_tool_call,
)
from .mcp_hub import MCPHub

logger = logging.getLogger(__name__)

# JSON Schema type -> Python type mapping for pydantic model generation.
_TYPE_MAP: dict[str, type] = {
    "string": str,
    "integer": int,
    "number": float,
    "boolean": bool,
    "object": dict,
    "array": list,
}


def _json_schema_to_pydantic(
    schema: dict[str, Any],
    model_name: str = "ToolInput",
) -> type[BaseModel]:
    """Convert an MCP tool's inputSchema to a Pydantic BaseModel.

    Handles required/optional fields and default values.
    """
    props = schema.get("properties", {})
    required = set(schema.get("required", []))
    fields: dict[str, Any] = {}

    for name, prop in props.items():
        # anyOf (e.g. FileAttachment | string) - use Any
        if "anyOf" in prop:
            py_type = Any
        else:
            py_type = _TYPE_MAP.get(prop.get("type", "string"), str)
        description = prop.get("description", "")
        default_val = prop.get("default")

        if name in required:
            fields[name] = (py_type, Field(description=description))
        elif default_val is not None:
            fields[name] = (py_type, Field(default=default_val, description=description))
        else:
            fields[name] = (py_type | None, Field(default=None, description=description))

    return create_model(model_name, **fields)


# Shared list: tool _arun stores produced files here for the UI to drain.
_produced_files: list[FileEntry] = []

# Status callback: set by __main__.py to update chat placeholder.
_status_callback: Any = None

# File notify callback: called immediately when a file is produced.
# Signature: (entry: FileEntry) -> None
_file_notify_callback: Any = None


def set_status_callback(fn: Any) -> None:
    """Register a callback for tool execution status updates."""
    global _status_callback
    _status_callback = fn


def set_file_notify_callback(fn: Any) -> None:
    """Register a callback for real-time file notifications."""
    global _file_notify_callback
    _file_notify_callback = fn


def _update_status(text: str) -> None:
    """Update the UI status indicator."""
    if _status_callback:
        try:
            _status_callback(text)
        except Exception:
            pass


def _notify_file(entry: FileEntry) -> None:
    """Notify the UI that a file was produced (real-time)."""
    if _file_notify_callback:
        try:
            _file_notify_callback(entry)
        except Exception:
            pass


def drain_produced_files() -> list[FileEntry]:
    """Pop all files produced since last drain."""
    files = list(_produced_files)
    _produced_files.clear()
    return files


def _make_mcp_tool_class(
    mcp_tool: MCPTool,
    hub: MCPHub,
    registry: FileRegistry,
) -> BaseTool:
    """Create a LangChain BaseTool instance for a single MCP tool.

    We dynamically create a subclass with the correct args_schema set as
    a class attribute (not a property) so LangChain's bind_tools can
    introspect it for the LLM.
    """
    schema = mcp_tool.inputSchema or {"type": "object", "properties": {}}
    safe_name = mcp_tool.name.replace("-", "_").replace(".", "_")
    input_model = _json_schema_to_pydantic(schema, f"{safe_name}_Input")

    class _Tool(BaseTool):
        name: str = mcp_tool.name
        description: str = mcp_tool.description or f"MCP tool: {mcp_tool.name}"
        args_schema: type[BaseModel] = input_model

        class Config:
            arbitrary_types_allowed = True

        def _run(self, **kwargs: Any) -> str:
            raise NotImplementedError("Use async")

        async def _arun(self, **kwargs: Any) -> str:
            logger.info("MCP tool call: %s(%s)", self.name, kwargs)
            _update_status(f"Calling {self.name} (MCP)...")

            # Pre-middleware: resolve file references from registry
            processed = pre_process_tool_call(
                mcp_tool.name, kwargs, mcp_tool, registry,
            )

            # Call MCP tool via hub (reconnects lazily if needed)
            result = await hub.call_tool(mcp_tool.name, processed)

            # Post-middleware: fetch produced files, register, sanitize
            agent = hub.agent_for_tool(mcp_tool.name)
            text, new_files = await post_process_tool_result(
                mcp_tool.name, result, agent, registry,
            )

            for entry in new_files:
                _produced_files.append(entry)
                _notify_file(entry)

            _update_status("")
            return text

    # Set a readable class name for debugging
    _Tool.__name__ = f"MCPTool_{safe_name}"
    _Tool.__qualname__ = f"MCPTool_{safe_name}"

    return _Tool()


def create_mcp_tools(
    hub: MCPHub,
    registry: FileRegistry,
) -> list[BaseTool]:
    """Create LangChain tool wrappers for all MCP tools from all agents."""
    tools: list[BaseTool] = []
    for mcp_tool in hub.all_tools():
        wrapper = _make_mcp_tool_class(mcp_tool, hub, registry)
        tools.append(wrapper)
        logger.info("Registered MCP tool: %s", mcp_tool.name)
    return tools
