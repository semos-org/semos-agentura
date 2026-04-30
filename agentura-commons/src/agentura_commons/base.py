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


@dataclass
class ToolDef:
    """Definition of a tool that the agent exposes via MCP."""

    name: str
    description: str
    fn: Any  # Callable - async or sync
    parameters: dict[str, Any] | None = None
    file_params: list[str] = field(default_factory=list)
    # MCP annotations (hints for client behavior)
    read_only: bool = False
    destructive: bool = False
    idempotent: bool = False
    # MCP execution hints
    task_support: str | None = None


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
