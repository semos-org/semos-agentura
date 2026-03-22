"""File registry and pre/post middleware for MCP tool calls.

Implements the client-side file handling from docs/file-handling-spec.md:
- LLM never sees binary data, only filenames
- Pre-middleware: resolve file references to base64 before sending to tools
- Post-middleware: detect download_url in results, fetch files, register them
"""

from __future__ import annotations

import base64
import json
import logging
from dataclasses import dataclass, field

import httpx
from mcp.types import CallToolResult, Tool

from .mcp_hub import AgentConnection

logger = logging.getLogger(__name__)

# Tool parameters known to accept files (fallback when x-file annotation absent).
_KNOWN_FILE_PARAMS = {"source", "file_path"}


@dataclass
class FileEntry:
    """A file tracked in the registry."""

    filename: str
    blob: bytes
    mime: str
    size: int
    source: str  # "upload" or "tool:<tool_name>"


class FileRegistry:
    """In-memory mapping of filename -> FileEntry."""

    def __init__(self) -> None:
        self._files: dict[str, FileEntry] = {}
        self._counter: int = 0  # monotonic counter for ordering

    def register(
        self,
        filename: str,
        blob: bytes,
        mime: str,
        source: str,
    ) -> FileEntry:
        entry = FileEntry(
            filename=filename,
            blob=blob,
            mime=mime,
            size=len(blob),
            source=source,
        )
        self._files[filename] = entry
        self._counter += 1
        return entry

    def get(self, filename: str) -> FileEntry | None:
        return self._files.get(filename)

    def delete(self, filename: str) -> bool:
        """Remove a file from the registry. Returns True if found."""
        return self._files.pop(filename, None) is not None

    @property
    def count(self) -> int:
        return self._counter


def human_size(nbytes: int) -> str:
    """Format byte count as human-readable string."""
    for unit in ("B", "KB", "MB", "GB"):
        if nbytes < 1024:
            return f"{nbytes:.0f} {unit}" if unit == "B" else f"{nbytes:.1f} {unit}"
        nbytes /= 1024
    return f"{nbytes:.1f} TB"


# ---------------------------------------------------------------------------
# Pre-middleware: resolve file references before sending to MCP tool
# ---------------------------------------------------------------------------


def _identify_file_params(tool: Tool) -> set[str]:
    """Find parameters that accept file references.

    Detection layers (first match wins):
    1. x-file: true in JSON Schema property
    2. Description contains "file path or base64"
    3. Parameter name in _KNOWN_FILE_PARAMS
    """
    schema = tool.inputSchema or {}
    props = schema.get("properties", {})
    file_params: set[str] = set()
    for name, prop in props.items():
        if prop.get("x-file"):
            file_params.add(name)
        elif "file path or base64" in (prop.get("description") or "").lower():
            file_params.add(name)
        elif name in _KNOWN_FILE_PARAMS:
            file_params.add(name)
    return file_params


def pre_process_tool_call(
    tool_name: str,
    arguments: dict,
    mcp_tool: Tool,
    registry: FileRegistry,
) -> dict:
    """Replace filename references with base64 data URIs for file parameters."""
    file_params = _identify_file_params(mcp_tool)
    if not file_params:
        return arguments

    processed = dict(arguments)
    for param_name in file_params:
        value = processed.get(param_name)
        if not isinstance(value, str):
            continue
        entry = registry.get(value)
        if entry:
            b64 = base64.b64encode(entry.blob).decode()
            processed[param_name] = (
                f"data:{entry.mime};base64,{b64}"
            )
            logger.info(
                "Pre-middleware: resolved %s='%s' -> "
                "data URI (%s)",
                param_name, value, human_size(entry.size),
            )
        else:
            logger.warning(
                "Pre-middleware: '%s' NOT in registry. "
                "Keys: %s",
                value, list(registry._files.keys()),
            )
    return processed


# ---------------------------------------------------------------------------
# Post-middleware: detect download_url, fetch file, register
# ---------------------------------------------------------------------------


async def post_process_tool_result(
    tool_name: str,
    result: CallToolResult,
    agent: AgentConnection,
    registry: FileRegistry,
) -> tuple[str, list[FileEntry]]:
    """Process tool result: fetch files from download_url, register them.

    Returns (text_for_llm, new_file_entries).
    """
    if not result.content:
        return "", []

    text = result.content[0].text if hasattr(result.content[0], "text") else str(result.content[0])

    try:
        data = json.loads(text)
    except (json.JSONDecodeError, TypeError):
        return text, []

    if not isinstance(data, dict) or "download_url" not in data:
        return text, []

    url = data["download_url"]
    filename = data.get("filename", url.rsplit("/", 1)[-1])

    try:
        async with httpx.AsyncClient() as client:
            resp = await client.get(url, timeout=60.0)
            resp.raise_for_status()
            blob = resp.content
        mime = resp.headers.get("content-type", "application/octet-stream")
        entry = registry.register(filename, blob, mime, source=f"tool:{tool_name}")
        logger.info(
            "Post-middleware: fetched %s (%s) from %s",
            filename,
            human_size(entry.size),
            url,
        )

        # Build sanitized text for LLM (no raw URLs)
        sanitized = {
            k: v for k, v in data.items() if k != "download_url"
        }
        sanitized["produced_file"] = f"{filename} ({human_size(entry.size)})"
        return json.dumps(sanitized), [entry]
    except Exception:
        logger.exception("Post-middleware: failed to fetch %s", url)
        return text, []
