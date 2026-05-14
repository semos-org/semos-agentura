"""Base class for Semos Agentura agent services.

Each agent implements BaseAgentService to expose its tools via MCP and skills via A2A.
The transport module then wires everything into a single FastAPI app.
"""

from __future__ import annotations

import base64
import json
import logging
import mimetypes
import os
import uuid
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, TypedDict

from langchain_core.tools import BaseTool
from pydantic import PrivateAttr

logger = logging.getLogger(__name__)


class FileAttachment(TypedDict):
    """A file reference with its original name and content.

    Used in MCP tool parameters that accept file input.
    The content field accepts a file path, base64 string, or data URI.
    Client middleware resolves file registry names to content before sending.
    """

    name: str
    content: str


@dataclass
class NamedFile:
    """A file with an explicit display name.

    Use when the on-disk filename (often UUID-prefixed) differs from
    the name the user should see. If not used, the transport layer
    infers the name from Path.name.
    """

    path: Path
    name: str


@dataclass
class ToolResult:
    """Structured result from a tool function.

    Tool functions can return any Python type - the MCP wrapper
    normalizes it automatically:
        str        -> ToolResult(text=...)
        dict/list  -> ToolResult(data=...)
        Path       -> ToolResult(files=[...])
        NamedFile  -> ToolResult(files=[...])
        ToolResult -> pass through

    Use ToolResult directly only when returning multiple modalities
    (e.g., text + files, or data + files).
    """

    text: str = ""
    data: dict | list | None = None
    files: list[Path | NamedFile] = field(default_factory=list)


def _is_file_type(annotation: Any) -> bool:
    """Check if a type annotation involves FileAttachment."""
    import typing

    if annotation is FileAttachment:
        return True
    origin = typing.get_origin(annotation)
    args = typing.get_args(annotation)
    # Union types: FileAttachment | str, Optional[FileAttachment], etc.
    if origin is typing.Union or str(origin) == "typing.Union":
        return any(_is_file_type(a) for a in args)
    # list[FileAttachment]
    if origin is list:
        return any(_is_file_type(a) for a in args)
    # types.UnionType (X | Y syntax in 3.10+)
    if hasattr(annotation, "__args__"):
        return any(_is_file_type(a) for a in annotation.__args__)
    return False


def _detect_file_params(schema: type | None) -> list[str]:
    """Detect file parameters from a Pydantic model's field annotations.

    Checks for FileAttachment type hints and json_schema_extra with x-file.
    """
    if schema is None:
        return []
    result = []
    for name, field_info in schema.model_fields.items():
        if _is_file_type(field_info.annotation):
            result.append(name)
            continue
        extra = field_info.json_schema_extra
        if isinstance(extra, dict) and extra.get("x-file"):
            result.append(name)
    return result


# Keep ToolDef as a deprecated alias during migration
@dataclass
class ToolDef:
    """DEPRECATED: Use AgentTool instead. Will be removed in v0.5."""

    name: str
    description: str
    fn: Any
    parameters: dict[str, Any] | None = None
    file_params: list[str] = field(default_factory=list)
    read_only: bool = False
    destructive: bool = False
    idempotent: bool = False
    task_support: str | None = None


class AgentTool(BaseTool):
    """Tool with MCP annotations, file param auto-detection, and Pydantic validation.

    Subclass this for each tool, or use the @agent_tool decorator for simple cases.

    Example (subclass):
        class DigestInput(BaseModel):
            source: FileAttachment | str = Field(description="Document to digest")

        class DigestDocumentTool(AgentTool):
            name: str = "digest_document"
            description: str = "Digest a document into Markdown."
            args_schema: type[BaseModel] = DigestInput
            read_only: bool = True

            async def _arun(self, **kwargs) -> str:
                ...

    Example (decorator):
        @agent_tool(read_only=True)
        async def get_examples(category: str = "") -> str:
            \"\"\"List available examples.\"\"\"
            ...
    """

    # MCP annotations (hints for client behavior)
    read_only: bool = False
    destructive: bool = False
    idempotent: bool = False
    # MCP execution hints
    task_support: str | None = None
    # File params - None = auto-detect from args_schema, [] = explicitly none
    file_params: list[str] | None = None
    # Override for dynamic schemas (e.g., add_root with runtime-detected protocols)
    parameters_override: dict[str, Any] | None = None

    # Service reference (set by bind_service)
    _service: Any = PrivateAttr(default=None)

    def bind_service(self, service: BaseAgentService) -> AgentTool:
        """Bind this tool to a service instance. Returns self for chaining."""
        self._service = service
        return self

    @property
    def resolved_file_params(self) -> list[str]:
        """File parameter names, auto-detected if not explicitly set."""
        if self.file_params is not None:
            return self.file_params
        return _detect_file_params(self.args_schema)

    def get_input_schema(self) -> dict[str, Any]:
        """Return JSON Schema for this tool's input parameters.

        Uses parameters_override if set, otherwise generates from args_schema.
        """
        if self.parameters_override is not None:
            return self.parameters_override
        if self.args_schema is not None:
            schema = self.args_schema.model_json_schema()
            # Strip Pydantic metadata that LLM APIs don't expect
            schema.pop("$defs", None)
            schema.pop("title", None)
            return schema
        return {"type": "object", "properties": {}}

    def _run(self, *args: Any, **kwargs: Any) -> Any:
        raise NotImplementedError("Use async _arun()")

    async def _arun(self, *args: Any, **kwargs: Any) -> Any:
        raise NotImplementedError("Subclass must implement _arun()")


def agent_tool(
    _fn: Any = None,
    *,
    read_only: bool = False,
    destructive: bool = False,
    idempotent: bool = False,
    task_support: str | None = None,
    file_params: list[str] | None = None,
    name: str | None = None,
) -> Any:
    """Decorator to create an AgentTool from an async function.

    Usage:
        @agent_tool(read_only=True)
        async def search_emails(query: str, limit: int = 50) -> str:
            \"\"\"Search emails matching a query.\"\"\"
            ...

    The tool's name defaults to the function name. Description comes from
    the docstring. args_schema is auto-generated from the function signature.
    """
    from langchain_core.tools import StructuredTool

    def _wrap(fn: Any) -> AgentTool:
        tool_name = name or fn.__name__
        description = (fn.__doc__ or "").strip().split("\n")[0]

        # Create a StructuredTool to get the auto-generated args_schema
        st = StructuredTool.from_function(
            func=fn,
            name=tool_name,
            description=description,
            coroutine=fn,
        )

        class _DecoratedTool(AgentTool):
            pass

        # Build the tool instance
        tool = _DecoratedTool(
            name=tool_name,
            description=description,
            args_schema=st.args_schema,
            read_only=read_only,
            destructive=destructive,
            idempotent=idempotent,
            task_support=task_support,
            file_params=file_params,
        )

        # Store the original function for _arun
        _original_fn = fn

        async def _arun_impl(**kwargs: Any) -> Any:
            return await _original_fn(**kwargs)

        tool._arun = _arun_impl  # type: ignore[assignment]
        return tool

    if _fn is not None:
        return _wrap(_fn)
    return _wrap


@dataclass
class SkillDef:
    """Definition of a skill that the agent exposes via A2A."""

    id: str
    name: str
    description: str
    tags: list[str] = field(default_factory=list)
    examples: list[str] = field(default_factory=list)


class BaseAgentService(ABC):
    """Abstract base for all agent services.

    Subclass this and implement the abstract properties/methods.
    The transport layer reads these to wire up MCP + A2A automatically.
    """

    # Set by create_app() - available after app is built
    output_dir: Path | None = None
    base_url: str | None = None

    def file_url(self, filename: str) -> str:
        """Return the download URL for a file in the output directory."""
        return f"{self.base_url}/files/{filename}"

    # Legacy helper - kept for backwards compatibility during migration.
    # New tools should return Path or NamedFile directly.
    def file_response(self, path: Path, display_name: str | None = None) -> str:
        """Build a JSON response for a file-producing tool."""
        name = display_name or path.name
        mime, _ = mimetypes.guess_type(str(path))
        return json.dumps(
            {
                "download_url": self.file_url(path.name),
                "filename": name,
                "mime_type": mime or "application/octet-stream",
                "size_bytes": path.stat().st_size,
            },
            ensure_ascii=False,
        )

    def resolve_file(
        self,
        source: str,
        default_ext: str = ".bin",
        filename: str = "",
    ) -> Path:
        """Resolve a source string to a local file Path.

        Detects the format first, then resolves:
        1. data URI (data:mime;base64,...) -> decode and write to temp file
        2. raw base64 -> decode and write to temp file
        3. HTTP(S) URL -> fetch and write to temp file
        4. file path -> return as-is if it exists

        If filename is provided, the temp file preserves that name
        (important for downstream tools that infer type from name).
        """
        assert self.output_dir, "output_dir must be set before resolving files"

        # 1. data URI
        if source.startswith("data:"):
            _, _, encoded = source.partition(",")
            return self._write_decoded(encoded, default_ext, filename)

        # 2. HTTP(S) URL
        if source.startswith(("http://", "https://")):
            import httpx as _httpx

            resp = _httpx.get(source, timeout=30)
            resp.raise_for_status()
            ext = Path(source.rsplit("/", 1)[-1]).suffix or default_ext
            fn = filename or f"_fetch_{uuid.uuid4().hex[:8]}{ext}"
            tmp = self.output_dir / fn
            tmp.write_bytes(resp.content)
            return tmp

        # 3. Existing file path (only try for short strings that look like paths)
        if len(source) < 1024:
            p = Path(source)
            if p.exists():
                return p
            # Try relative to output_dir
            p2 = self.output_dir / source
            if p2.exists():
                return p2

        # 4. Raw base64 (no data: prefix)
        try:
            data = base64.b64decode(source, validate=True)
            if len(data) > 4:
                return self._write_bytes(data, default_ext, filename)
        except Exception:
            pass

        # Fallback: return as path (may not exist - tool will error)
        return Path(source)

    def _write_decoded(
        self,
        encoded: str,
        default_ext: str,
        filename: str,
    ) -> Path:
        """Decode base64 and write to a temp file."""
        data = base64.b64decode(encoded)
        return self._write_bytes(data, default_ext, filename)

    def _write_bytes(
        self,
        data: bytes,
        default_ext: str,
        filename: str,
    ) -> Path:
        """Write bytes to a temp file, preserving filename if given."""
        if filename:
            subdir = self.output_dir / f"_att_{uuid.uuid4().hex[:8]}"
            subdir.mkdir(exist_ok=True)
            tmp = subdir / filename
        else:
            tmp = self.output_dir / f"_upload_{uuid.uuid4().hex[:8]}{default_ext}"
        tmp.write_bytes(data)
        return tmp

    def resolve_file_attachment(
        self,
        source: FileAttachment | str,
        default_ext: str = ".bin",
    ) -> Path:
        """Resolve a FileAttachment dict or plain string to a local Path."""
        if isinstance(source, dict):
            name = source.get("name", "")
            content = source.get("content", name)
            ext = Path(name).suffix if name else default_ext
            return self.resolve_file(content, default_ext=ext, filename=name)
        return self.resolve_file(source, default_ext=default_ext)

    @property
    @abstractmethod
    def agent_name(self) -> str:
        """Human-readable agent name (e.g. 'Email Agent')."""

    @property
    @abstractmethod
    def agent_description(self) -> str:
        """Short description of what this agent does."""

    @property
    def agent_version(self) -> str:
        return "0.1.0"

    @property
    def agent_system_prompt(self) -> str:
        """Agent-specific system prompt, appended to LLMExecutor's base prompt.

        Default: current date/time. Override in subclasses:
        - Extend: return super().agent_system_prompt + "\\n\\nExtra instructions.".
        - Replace: return "Custom prompt." (omits date/time).
        """
        from datetime import datetime

        now = datetime.now().astimezone()
        tz = now.strftime("%Z") or str(now.tzinfo)
        return now.strftime(f"Current date/time: %Y-%m-%d %H:%M (%A) {tz}.")

    # Optional LLM router for A2A natural language dispatch.
    # Set ROUTER_LLM_MODEL, ROUTER_LLM_API_KEY, ROUTER_LLM_API_BASE
    # in .env to enable. Override in subclass for custom logic.
    @property
    def router_llm_model(self) -> str:
        return os.environ.get("ROUTER_LLM_MODEL", "")

    @property
    def router_llm_api_key(self) -> str:
        return os.environ.get("ROUTER_LLM_API_KEY", "")

    @property
    def router_llm_api_base(self) -> str:
        return os.environ.get("ROUTER_LLM_API_BASE", "")

    @abstractmethod
    def get_tools(self) -> list[ToolDef]:
        """Return all MCP tools this agent exposes."""

    @abstractmethod
    def get_skills(self) -> list[SkillDef]:
        """Return all A2A skills this agent exposes."""

    @abstractmethod
    async def execute_skill(
        self,
        skill_id: str,
        message: str,
        *,
        task_id: str | None = None,
    ) -> str:
        """Execute an A2A skill by ID with the given message."""
