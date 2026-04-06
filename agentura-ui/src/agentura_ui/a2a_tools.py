"""LangChain BaseTool wrappers for A2A tool/skill execution.

Mirrors mcp_tools.py but routes calls through A2A instead of MCP SSE.
Uses the same file middleware (pre_process_tool_call for input,
download_url fetching for output).
"""

from __future__ import annotations

import base64
import logging
from typing import Any

from langchain_core.tools import BaseTool
from mcp.types import Tool as MCPTool
from pydantic import BaseModel, Field, create_model

from .a2a_client import A2AAgentInfo, FileInfo, send_task, send_tool_call
from .file_registry import (
    FileEntry,
    FileRegistry,
    pre_process_tool_call,
)
from .mcp_tools import (
    _json_schema_to_pydantic,
    _notify_file,
    _produced_files,
    _update_status,
)

logger = logging.getLogger(__name__)


async def _fetch_a2a_files(
    file_infos: list[FileInfo],
    tool_name: str,
    registry: FileRegistry,
) -> list[FileEntry]:
    """Fetch files from A2A artifact URLs and register them."""
    import httpx

    new_files: list[FileEntry] = []
    for info in file_infos:
        try:
            async with httpx.AsyncClient() as client:
                resp = await client.get(info.url, timeout=60.0)
                resp.raise_for_status()
            mime = (
                resp.headers.get("content-type")
                or info.mime_type
                or "application/octet-stream"
            )
            entry = registry.register(
                info.filename, resp.content, mime,
                source=f"tool:{tool_name}",
            )
            new_files.append(entry)
            logger.info(
                "A2A post: fetched %s from %s",
                info.filename, info.url,
            )
        except Exception:
            logger.exception(
                "A2A post: failed to fetch %s", info.url,
            )
    return new_files


def _resolve_files_for_send(
    message: str,
    registry: FileRegistry,
) -> list[dict] | None:
    """Find registered filenames in message text.

    Returns list of FileAttachment dicts {name, content} with
    base64 data URIs, or None if no matches.
    """
    matched: list[dict] = []
    for entry in registry.files.values():
        if entry.filename in message:
            b64 = base64.b64encode(entry.blob).decode()
            matched.append({
                "name": entry.filename,
                "content": f"data:{entry.mime};base64,{b64}",
            })
            logger.info(
                "A2A delegate: attaching %s (%d bytes)",
                entry.filename, entry.size,
            )
    return matched or None


def _make_a2a_tool_class(
    mcp_tool: MCPTool,
    a2a_info: A2AAgentInfo,
    registry: FileRegistry,
) -> BaseTool:
    """Create a LangChain BaseTool that executes via A2A.

    Uses the same name/description/args_schema as the MCP wrapper
    so the LLM sees identical tools regardless of protocol.
    """
    schema = mcp_tool.inputSchema or {
        "type": "object", "properties": {},
    }
    safe_name = mcp_tool.name.replace("-", "_").replace(".", "_")
    input_model = _json_schema_to_pydantic(
        schema, f"{safe_name}_A2AInput",
    )

    class _Tool(BaseTool):
        name: str = mcp_tool.name
        description: str = (
            mcp_tool.description or f"A2A tool: {mcp_tool.name}"
        )
        args_schema: type[BaseModel] = input_model

        class Config:
            arbitrary_types_allowed = True

        def _run(self, **kwargs: Any) -> str:
            raise NotImplementedError("Use async")

        async def _arun(self, **kwargs: Any) -> str:
            logger.info(
                "A2A tool call: %s(%s)", self.name, kwargs,
            )
            _update_status(
                f"Calling {self.name} on "
                f"{a2a_info.name} (A2A)...",
            )

            # Pre-middleware: same file resolution as MCP
            processed = pre_process_tool_call(
                mcp_tool.name, kwargs, mcp_tool, registry,
            )

            # Call tool via A2A
            result = await send_tool_call(
                a2a_info, mcp_tool.name, processed,
            )
            logger.info(
                "A2A tool %s response: status=%s "
                "text=%s files=%d",
                self.name, result.status,
                result.text[:200] if result.text else "",
                len(result.files),
            )

            # Post: fetch produced files from download URLs
            _update_status(
                f"Fetching results from {a2a_info.name}...",
            )
            new_files = await _fetch_a2a_files(
                result.files, mcp_tool.name, registry,
            )
            for entry in new_files:
                _produced_files.append(entry)
                _notify_file(entry)

            _update_status("")
            return result.text

    _Tool.__name__ = f"A2ATool_{safe_name}"
    _Tool.__qualname__ = f"A2ATool_{safe_name}"
    return _Tool()


def _make_a2a_delegate_tool(
    a2a_info: A2AAgentInfo,
    registry: FileRegistry,
) -> BaseTool:
    """Create an 'ask_<agent>' tool for natural-language delegation.

    The agent decides which tools to call based on the message.
    Returns format_for_llm() text with status/task_id hints.
    """
    slug = (
        a2a_info.name.lower()
        .replace(" ", "_")
        .replace("-", "_")
    )
    tool_name = f"ask_{slug}"
    desc = (
        f"Send a natural-language task to {a2a_info.name}. "
        f"The agent decides which tools to use. "
        f"If the agent needs more info it will ask - "
        f"continue with the same context_id. "
        f"{a2a_info.description}"
    )

    input_model = create_model(
        f"{tool_name}_Input",
        message=(str, Field(description="Task description")),
        context_id=(
            str | None,
            Field(
                default=None,
                description=(
                    "Continue a conversation with the agent "
                    "(context_id from a previous response)"
                ),
            ),
        ),
    )

    class _Tool(BaseTool):
        name: str = tool_name
        description: str = desc
        args_schema: type[BaseModel] = input_model

        class Config:
            arbitrary_types_allowed = True

        def _run(self, **kwargs: Any) -> str:
            raise NotImplementedError("Use async")

        async def _arun(self, **kwargs: Any) -> str:
            message = kwargs.get("message", "")
            context_id = kwargs.get("context_id")
            logger.info(
                "A2A delegate: %s(%s, context_id=%s)",
                self.name, message, context_id,
            )
            # Show agent name in status while working
            short_msg = (
                message[:60] + "..."
                if len(message) > 60 else message
            )
            _update_status(
                f"{a2a_info.name}: {short_msg}",
            )

            # Resolve files mentioned in message text
            file_dicts = _resolve_files_for_send(
                message, registry,
            )

            result = await send_task(
                a2a_info, message,
                files=file_dicts,
                context_id=context_id,
            )
            logger.info(
                "A2A delegate %s response: "
                "status=%s task_id=%s text=%s files=%d",
                self.name, result.status,
                result.task_id,
                result.text[:200] if result.text else "",
                len(result.files),
            )

            new_files = await _fetch_a2a_files(
                result.files, self.name, registry,
            )
            for entry in new_files:
                _produced_files.append(entry)
                _notify_file(entry)

            _update_status("")

            # Format for LLM with status/task_id hints
            from agentura_commons.client import ClientA2AResult
            llm_result = ClientA2AResult(
                text=result.text,
                agent_name=a2a_info.name,
                files=new_files,
                task_id=result.task_id,
                context_id=result.context_id,
                status=result.status,
            )
            return llm_result.format_for_llm()

    _Tool.__name__ = f"A2ADelegate_{slug}"
    _Tool.__qualname__ = f"A2ADelegate_{slug}"
    return _Tool()


def create_a2a_tools(
    a2a_info: A2AAgentInfo,
    mcp_tools: list[MCPTool],
    registry: FileRegistry,
) -> tuple[dict[str, BaseTool], BaseTool]:
    """Create A2A tool wrappers + delegate tool for an agent."""
    wrappers: dict[str, BaseTool] = {}
    for mcp_tool in mcp_tools:
        tool = _make_a2a_tool_class(
            mcp_tool, a2a_info, registry,
        )
        wrappers[mcp_tool.name] = tool
        logger.info("Registered A2A tool: %s", mcp_tool.name)

    delegate = _make_a2a_delegate_tool(a2a_info, registry)
    logger.info("Registered A2A delegate: %s", delegate.name)

    return wrappers, delegate


def create_a2a_delegates(
    a2a_agents: list[A2AAgentInfo],
    registry: FileRegistry,
) -> list[BaseTool]:
    """Create one delegate tool per A2A agent."""
    delegates: list[BaseTool] = []
    for agent in a2a_agents:
        delegate = _make_a2a_delegate_tool(agent, registry)
        delegates.append(delegate)
        logger.info(
            "Registered A2A delegate: %s", delegate.name,
        )
    return delegates
