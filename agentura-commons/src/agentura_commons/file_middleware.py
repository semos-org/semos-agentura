"""Client-side file middleware for MCP tool calls.

Implements the client half of docs/file-handling-spec.md:
- LLM never sees binary data or URLs, only filenames
- Pre-middleware: resolve file references to base64 before sending to tools
- Post-middleware: scan ResourceLink blocks in results, fetch and register files

Extracted from agentura-ui/file_registry.py (protocol-level parts only).
"""

from __future__ import annotations

import base64
import json
import logging
from dataclasses import dataclass

import httpx
from mcp.types import CallToolResult, Tool

from .mcp_client import AgentConnection

logger = logging.getLogger(__name__)


class FileNotResolvedError(Exception):
    """Raised when a file reference cannot be resolved from the registry or VFS."""


# Tool parameters known to accept files (fallback when x-file annotation absent).
# Removed: _KNOWN_FILE_PARAMS heuristic was too risky (matched
# "source" in compose_document which accepts plain text)


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
        self._counter: int = 0

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
        entry = self._files.get(filename)
        if entry:
            return entry
        for key, entry in self._files.items():
            if key.endswith(filename) or filename.endswith(key):
                return entry
        return None

    def delete(self, filename: str) -> bool:
        return self._files.pop(filename, None) is not None

    @property
    def files(self) -> dict[str, FileEntry]:
        return self._files

    @property
    def count(self) -> int:
        return self._counter


def human_size(nbytes: int) -> str:
    """Format byte count as human-readable string."""
    for unit in ("B", "KB", "MB", "GB"):
        if nbytes < 1024:
            if unit == "B":
                return f"{nbytes:.0f} {unit}"
            return f"{nbytes:.1f} {unit}"
        nbytes /= 1024
    return f"{nbytes:.1f} TB"


# Pre-middleware


def _identify_file_params(tool: Tool) -> set[str]:
    """Find top-level parameters that accept file references.

    Only uses explicit x-file annotations in JSON Schema.
    No heuristics - tool authors must mark file params with x-file.
    """
    schema = tool.inputSchema or {}
    props = schema.get("properties", {})
    defs = schema.get("$defs", {})
    file_params: set[str] = set()
    for name, prop in props.items():
        if prop.get("x-file"):
            file_params.add(name)
        elif _has_nested_file_fields(prop, defs):
            file_params.add(name)
    return file_params


def _has_nested_file_fields(prop: dict, defs: dict) -> bool:
    """Check if a property contains nested x-file fields (e.g., array of objects)."""
    # Direct $ref
    ref = prop.get("$ref", "")
    if ref:
        ref_name = ref.rsplit("/", 1)[-1]
        ref_schema = defs.get(ref_name, {})
        return _schema_has_x_file(ref_schema)

    # anyOf (Union types)
    for variant in prop.get("anyOf", []):
        if _has_nested_file_fields(variant, defs):
            return True

    # Array items
    items = prop.get("items", {})
    if items:
        if items.get("x-file"):
            return True
        ref = items.get("$ref", "")
        if ref:
            ref_name = ref.rsplit("/", 1)[-1]
            ref_schema = defs.get(ref_name, {})
            return _schema_has_x_file(ref_schema)

    return False


def _schema_has_x_file(schema: dict) -> bool:
    """Check if any property in a schema object has x-file: true."""
    for prop in schema.get("properties", {}).values():
        if prop.get("x-file"):
            return True
    return False


def _has_file_attachment_schema(tool: Tool, param: str) -> bool:
    """Check if the param's schema uses the FileAttachment type."""
    schema = tool.inputSchema or {}
    prop = schema.get("properties", {}).get(param, {})
    for variant in prop.get("anyOf", []):
        if "$ref" in variant and "FileAttachment" in variant["$ref"]:
            return True
        items = variant.get("items", {})
        if "$ref" in items and "FileAttachment" in items["$ref"]:
            return True
    return False


def strip_vfs_prefix(name: str) -> str:
    """Strip client-internal URI prefixes before sending over the wire.

    session://report.pdf -> report.pdf
    local://docs/file.txt -> docs/file.txt
    http://example.com/f -> http://example.com/f (unchanged)
    report.pdf -> report.pdf (unchanged)
    """
    if "://" in name and not name.startswith(
        ("http://", "https://", "data:"),
    ):
        return name.split("://", 1)[1].lstrip("/")
    return name


def _make_file_attachment(entry: FileEntry) -> dict:
    """Build a FileAttachment dict {name, content} for the wire.

    Strips VFS URI prefixes so agents receive clean filenames.
    """
    name = strip_vfs_prefix(entry.filename)
    b64 = base64.b64encode(entry.blob).decode()
    return {
        "name": name,
        "content": f"data:{entry.mime};base64,{b64}",
    }


def _resolve_attachment_item(
    item: dict | str,
    registry: FileRegistry,
    param_name: str,
) -> dict:
    """Resolve a single FileAttachment dict or string."""
    if isinstance(item, str):
        entry = registry.get(item)
        if entry:
            logger.info(
                "Pre-middleware: resolved %s='%s' (%s)",
                param_name,
                entry.filename,
                human_size(entry.size),
            )
            return _make_file_attachment(entry)
        return {"name": item, "content": item}

    if not isinstance(item, dict):
        return item

    fname = item.get("name", "")
    content = item.get("content", "")
    entry = registry.get(fname)
    if not entry:
        clean = content.split(" (")[0].strip()
        entry = registry.get(clean)
    if not entry:
        entry = registry.get(content)
    if entry:
        logger.info(
            "Pre-middleware: resolved attachment %s='%s' (%s)",
            param_name,
            entry.filename,
            human_size(entry.size),
        )
        return _make_file_attachment(entry)
    return item


def _is_array_param(schema: dict, param_name: str) -> bool:
    """Check if a param is typed as an array in the schema."""
    prop = schema.get("properties", {}).get(param_name, {})
    if prop.get("type") == "array":
        return True
    for variant in prop.get("anyOf", []):
        if variant.get("type") == "array":
            return True
    return False


def _get_array_item_schema(
    schema: dict,
    param_name: str,
    defs: dict,
) -> dict:
    """Get the resolved item schema for an array parameter."""
    prop = schema.get("properties", {}).get(param_name, {})
    items = prop.get("items", {})
    # Check anyOf for the array variant
    if not items:
        for variant in prop.get("anyOf", []):
            if variant.get("type") == "array":
                items = variant.get("items", {})
                break
    # Resolve $ref
    ref = items.get("$ref", "")
    if ref:
        ref_name = ref.rsplit("/", 1)[-1]
        return defs.get(ref_name, {})
    return items


def _get_file_fields_in_schema(item_schema: dict) -> set[str]:
    """Find field names with x-file: true in an object schema."""
    result = set()
    for name, prop in item_schema.get("properties", {}).items():
        if prop.get("x-file"):
            result.add(name)
    return result


def _resolve_item_deep(
    item: dict | str,
    registry: FileRegistry,
    param_name: str,
    item_schema: dict,
) -> dict | str:
    """Resolve file references in an array item, including nested x-file fields.

    Handles:
    - Plain string: resolve as filename from registry
    - Dict with nested x-file fields: resolve each file field
    - Dict that IS a FileAttachment: resolve as before
    """
    # String: could be a filename reference
    if isinstance(item, str):
        entry = registry.get(item)
        if entry:
            logger.info(
                "Pre-middleware: resolved nested %s item '%s' (%s)",
                param_name,
                entry.filename,
                human_size(entry.size),
            )
            return _make_file_attachment(entry)
        return item

    if not isinstance(item, dict):
        return item

    # Check if this dict has nested x-file fields to resolve
    file_fields = _get_file_fields_in_schema(item_schema)
    if file_fields:
        resolved = dict(item)
        for field_name in file_fields:
            val = resolved.get(field_name)
            if not isinstance(val, str) or not val:
                # Use 'name' field as content source if content is empty
                if field_name == "content" and not val:
                    val = resolved.get("name", "")
                if not val:
                    continue
            entry = registry.get(val)
            if entry:
                b64 = base64.b64encode(entry.blob).decode()
                resolved[field_name] = f"data:{entry.mime};base64,{b64}"
                logger.info(
                    "Pre-middleware: resolved %s.%s='%s' (%s)",
                    param_name,
                    field_name,
                    entry.filename,
                    human_size(entry.size),
                )
        return resolved

    # Fallback: treat as a regular FileAttachment-like dict
    return _resolve_attachment_item(item, registry, param_name)


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
    """
    file_params = _identify_file_params(mcp_tool)
    if not file_params:
        return arguments

    schema = mcp_tool.inputSchema or {}
    defs = schema.get("$defs", {})
    processed = dict(arguments)
    for param_name in file_params:
        value = processed.get(param_name)
        uses_attachment = _has_file_attachment_schema(
            mcp_tool,
            param_name,
        )

        # LLM may pass a plain string for an array param - normalize
        if isinstance(value, str) and _is_array_param(schema, param_name):
            # Try JSON parse: '["file.png"]' or '[{"name": "file.png"}]'
            try:
                parsed = json.loads(value)
                if isinstance(parsed, list):
                    value = parsed
            except (json.JSONDecodeError, TypeError):
                # Plain filename string - wrap as single-item list
                value = [value]
            processed[param_name] = value

        # List: resolve each item (may be FileAttachment, nested object, or string)
        if isinstance(value, list):
            resolved_list = []
            item_schema = _get_array_item_schema(schema, param_name, defs)
            for item in value:
                resolved_item = _resolve_item_deep(
                    item,
                    registry,
                    param_name,
                    item_schema,
                )
                resolved_list.append(resolved_item)
            processed[param_name] = resolved_list
            continue

        # Single FileAttachment dict
        if isinstance(value, dict):
            processed[param_name] = _resolve_attachment_item(
                value,
                registry,
                param_name,
            )
            continue

        if not isinstance(value, str):
            continue

        # Exact filename match
        entry = registry.get(value)
        if entry:
            if uses_attachment:
                processed[param_name] = _make_file_attachment(entry)
            else:
                b64 = base64.b64encode(entry.blob).decode()
                processed[param_name] = f"data:{entry.mime};base64,{b64}"
            logger.info(
                "Pre-middleware: resolved %s='%s' (%s)",
                param_name,
                value,
                human_size(entry.size),
            )
        else:
            # Try embedded refs (markdown with filenames)
            if registry.files:
                resolved = _resolve_embedded_refs(value, registry)
                if resolved != value:
                    processed[param_name] = resolved
                    continue
            # File param unresolved - flag as error so the LLM
            # gets a clear message instead of a cryptic ENOENT
            logger.warning(
                "Pre-middleware: unresolved file param %s='%s'",
                param_name,
                value,
            )
            raise FileNotResolvedError(
                f"File '{value}' not found for parameter '{param_name}'. "
                f"Make sure the file was uploaded or produced by a previous tool. "
                f"For files on mounted drives, use the full VFS path "
                f"(e.g., local://path/to/file). "
                f"Use list_files to check available files."
            )
    return processed


def _resolve_embedded_refs(
    value: str,
    registry: FileRegistry,
) -> str:
    """Replace registered filenames embedded in longer text.

    Handles markdown image refs like ![alt](filename.png) and
    bare filenames in prose. Used when a tool parameter contains
    markdown referencing registered files (e.g. compose_document
    with source containing ![](diagram.png)).
    """
    import re

    # 1. Markdown image/link refs: ![alt](filename) or [text](filename)
    def _replace_md_ref(match: re.Match) -> str:
        bracket = match.group("bracket")
        ref = match.group("ref")
        entry = registry.get(ref)
        if not entry:
            return match.group(0)
        b64 = base64.b64encode(entry.blob).decode()
        data_uri = f"data:{entry.mime};base64,{b64}"
        logger.info(
            "Embedded ref: resolved '%s' (%s)",
            ref,
            human_size(entry.size),
        )
        return f"{bracket}({data_uri})"

    pattern = r"(?P<bracket>!?\[[^\]]*\])\((?P<ref>[^)]+)\)"
    resolved = re.sub(pattern, _replace_md_ref, value)
    if resolved != value:
        return resolved

    # 2. Bare filename matches anywhere in the text
    for fname, entry in registry.files.items():
        if fname in value:
            b64 = base64.b64encode(entry.blob).decode()
            data_uri = f"data:{entry.mime};base64,{b64}"
            value = value.replace(fname, data_uri)
            logger.info(
                "Embedded ref: resolved bare '%s' (%s)",
                fname,
                human_size(entry.size),
            )
    return value


# Post-middleware


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
            "content-type",
            "application/octet-stream",
        )
        entry = registry.register(
            filename,
            resp.content,
            mime,
            source=f"tool:{tool_name}",
        )
        logger.info(
            "Post-middleware: fetched %s (%s) from %s",
            filename,
            human_size(entry.size),
            url,
        )
        return entry
    except Exception:
        logger.exception(
            "Post-middleware: failed to fetch %s",
            url,
        )
        return None


def _rewrite_url(url: str | object, base_url: str) -> str:
    """Normalize a file URL to the agent's actual base_url."""
    url = str(url)
    if url.startswith("/"):
        return f"{base_url}{url}"
    if url.startswith("http"):
        path = "/" + url.split("/", 3)[-1]
        return f"{base_url}{path}"
    return url


async def post_process_tool_result(
    tool_name: str,
    result: CallToolResult,
    agent: AgentConnection,
    registry: FileRegistry,
) -> tuple[str, list[FileEntry]]:
    """Process tool result: fetch files from ResourceLinks, register them.

    Returns (text_for_llm, new_file_entries).

    Scans result.content for ResourceLink blocks (MCP spec).
    Each ResourceLink is fetched and registered in the file registry.
    The LLM sees symbolic filenames, never URLs.
    """
    if not result.content:
        return "", []

    base_url = agent.base_url
    new_files: list[FileEntry] = []
    text_parts: list[str] = []

    # Scan content blocks
    for block in result.content:
        if hasattr(block, "uri") and block.uri:
            # ResourceLink: fetch and register
            url = _rewrite_url(block.uri, base_url)
            entry = await _fetch_and_register(
                url,
                getattr(block, "name", None),
                tool_name,
                registry,
            )
            if entry:
                new_files.append(entry)
        elif hasattr(block, "text"):
            text_parts.append(block.text)

    # Build LLM-facing text
    if new_files:
        # Get structured metadata if available
        sc = getattr(result, "structuredContent", None)
        if sc and isinstance(sc, dict):
            data = dict(sc)
            # Remove file URLs from structured data
            data.pop("download_url", None)
            data.pop("mime_type", None)
            data.pop("size_bytes", None)
        elif text_parts:
            try:
                data = json.loads(text_parts[0])
                if not isinstance(data, dict):
                    data = {}
            except (json.JSONDecodeError, TypeError):
                data = {}
        else:
            data = {}

        # Add file summaries for the LLM
        summaries = [f"{e.filename} ({human_size(e.size)})" for e in new_files]
        if len(summaries) == 1:
            data["produced_file"] = summaries[0]
        else:
            data["produced_files"] = summaries

        return json.dumps(data, ensure_ascii=False), new_files

    # No files - return text as-is
    return "\n".join(text_parts), []
