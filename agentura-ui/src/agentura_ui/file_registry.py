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
        """Look up a file by name. Tries exact match first,
        then falls back to suffix/substring matching for cases
        where the LLM drops UUID prefixes or paraphrases names.
        """
        # Exact match
        entry = self._files.get(filename)
        if entry:
            return entry
        # Suffix match: "iter_01.png" matches "5e922231_iter_01.png"
        for key, entry in self._files.items():
            if key.endswith(filename) or filename.endswith(key):
                return entry
        return None

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


def _resolve_embedded_refs(value: str, registry: FileRegistry) -> str:
    """Resolve embedded file references inside a string value.

    Handles markdown like ![alt](filename.png) and also bare
    filenames that appear in longer text. Used when a tool
    parameter contains markdown with references to registered
    files (e.g. compose_document with source=<markdown>).
    """
    from .renderers import resolve_file_references

    resolved = resolve_file_references(value, registry)
    if resolved != value:
        return resolved

    # Also try bare filename matches anywhere in the text
    for fname, entry in registry._files.items():
        if fname in value:
            b64 = base64.b64encode(entry.blob).decode()
            data_uri = f"data:{entry.mime};base64,{b64}"
            value = value.replace(fname, data_uri)
            logger.info(
                "Pre-middleware: resolved embedded '%s' "
                "(%s)",
                fname, human_size(entry.size),
            )
    return value


def _has_file_attachment_schema(mcp_tool: Tool, param: str) -> bool:
    """Check if the param's schema uses the FileAttachment type."""
    schema = mcp_tool.inputSchema or {}
    prop = schema.get("properties", {}).get(param, {})
    for variant in prop.get("anyOf", []):
        if "$ref" in variant and "FileAttachment" in variant["$ref"]:
            return True
        # Array of FileAttachment
        items = variant.get("items", {})
        if "$ref" in items and "FileAttachment" in items["$ref"]:
            return True
    return False


def _make_file_attachment(
    filename: str, entry: FileEntry,
) -> dict:
    """Build a FileAttachment dict {name, content} for MCP."""
    b64 = base64.b64encode(entry.blob).decode()
    return {
        "name": filename,
        "content": f"data:{entry.mime};base64,{b64}",
    }


def pre_process_tool_call(
    tool_name: str,
    arguments: dict,
    mcp_tool: Tool,
    registry: FileRegistry,
) -> dict:
    """Replace filename references with resolved file content.

    For params with FileAttachment schema (x-file + anyOf),
    produces {"name": filename, "content": data_uri}.
    For plain string params, replaces with the data URI directly.
    Also resolves embedded markdown file references in longer text.
    """
    file_params = _identify_file_params(mcp_tool)
    if not file_params:
        return arguments

    processed = dict(arguments)
    for param_name in file_params:
        value = processed.get(param_name)
        uses_attachment = _has_file_attachment_schema(
            mcp_tool, param_name,
        )

        # List of FileAttachments (e.g. create_draft attachments)
        if isinstance(value, list):
            resolved_list = []
            for item in value:
                resolved_item = _resolve_attachment_item(
                    item, registry, param_name,
                )
                resolved_list.append(resolved_item)
            processed[param_name] = resolved_list
            continue

        # Single FileAttachment dict from the LLM
        if isinstance(value, dict):
            processed[param_name] = _resolve_attachment_item(
                value, registry, param_name,
            )
            continue

        if not isinstance(value, str):
            continue

        # Exact filename match
        entry = registry.get(value)
        if entry:
            if uses_attachment:
                processed[param_name] = _make_file_attachment(
                    entry.filename, entry,
                )
            else:
                b64 = base64.b64encode(entry.blob).decode()
                processed[param_name] = (
                    f"data:{entry.mime};base64,{b64}"
                )
            logger.info(
                "Pre-middleware: resolved %s='%s' (%s)",
                param_name, value, human_size(entry.size),
            )
        elif registry._files:
            # Not an exact filename - might be markdown or
            # other text with embedded file references.
            resolved = _resolve_embedded_refs(value, registry)
            if resolved != value:
                processed[param_name] = resolved
    return processed


def _resolve_attachment_item(
    item: dict | str, registry: FileRegistry, param_name: str,
) -> dict:
    """Resolve a single FileAttachment dict or string."""
    if isinstance(item, str):
        entry = registry.get(item)
        if entry:
            logger.info(
                "Pre-middleware: resolved %s='%s' (%s)",
                param_name, entry.filename,
                human_size(entry.size),
            )
            return _make_file_attachment(entry.filename, entry)
        return {"name": item, "content": item}

    if not isinstance(item, dict):
        return item

    # Try name field, then content field for registry lookup
    fname = item.get("name", "")
    content = item.get("content", "")
    entry = registry.get(fname)
    if not entry:
        # Content might be "filename (size)" from post-middleware
        # or just the filename
        clean = content.split(" (")[0].strip()
        entry = registry.get(clean)
    if not entry:
        entry = registry.get(content)
    if entry:
        logger.info(
            "Pre-middleware: resolved attachment "
            "%s='%s' (%s)",
            param_name, entry.filename,
            human_size(entry.size),
        )
        return _make_file_attachment(entry.filename, entry)
    return item


# ---------------------------------------------------------------------------
# Post-middleware: detect download_url, fetch file, register
# ---------------------------------------------------------------------------


async def _fetch_and_register(
    url: str,
    filename: str | None,
    tool_name: str,
    registry: FileRegistry,
) -> FileEntry | None:
    """Fetch a file from a download URL and register it."""
    if not filename:
        filename = url.rsplit("/", 1)[-1]
    try:
        async with httpx.AsyncClient() as client:
            resp = await client.get(url, timeout=60.0)
            resp.raise_for_status()
        mime = resp.headers.get(
            "content-type", "application/octet-stream",
        )
        entry = registry.register(
            filename, resp.content, mime,
            source=f"tool:{tool_name}",
        )
        logger.info(
            "Post-middleware: fetched %s (%s) from %s",
            filename, human_size(entry.size), url,
        )
        return entry
    except Exception:
        logger.exception(
            "Post-middleware: failed to fetch %s", url,
        )
        return None


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

    if not isinstance(data, dict):
        return text, []

    new_files: list[FileEntry] = []

    # Top-level download_url (compose_document, generate_diagram, fill_form)
    if "download_url" in data:
        entry = await _fetch_and_register(
            data["download_url"],
            data.get("filename"),
            tool_name, registry,
        )
        if entry:
            new_files.append(entry)
            data.pop("download_url", None)
            data["produced_file"] = (
                f"{entry.filename} ({human_size(entry.size)})"
            )

    # Nested download_url in attachments list (read_email)
    for att in data.get("attachments", []):
        if isinstance(att, dict) and "download_url" in att:
            entry = await _fetch_and_register(
                att["download_url"],
                att.get("filename"),
                tool_name, registry,
            )
            if entry:
                new_files.append(entry)
                att.pop("download_url", None)
                att.pop("saved_path", None)
                att["registered_file"] = (
                    f"{entry.filename} "
                    f"({human_size(entry.size)})"
                )

    if new_files:
        return json.dumps(data, ensure_ascii=False), new_files
    return text, []
