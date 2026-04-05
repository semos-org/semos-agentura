"""Base class for Semos Agentura agent services.

Each agent implements BaseAgentService to expose its tools via MCP and skills via A2A.
The transport module then wires everything into a single FastAPI app.
"""

from __future__ import annotations

import json
import mimetypes
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, TypedDict


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
        return json.dumps({
            "download_url": self.file_url(path.name),
            "filename": name,
            "mime_type": mime or "application/octet-stream",
            "size_bytes": path.stat().st_size,
        }, ensure_ascii=False)

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

    @abstractmethod
    def get_tools(self) -> list[ToolDef]:
        """Return all MCP tools this agent exposes."""

    @abstractmethod
    def get_skills(self) -> list[SkillDef]:
        """Return all A2A skills this agent exposes."""

    @abstractmethod
    async def execute_skill(
        self, skill_id: str, message: str,
        *, task_id: str | None = None,
    ) -> str:
        """Execute an A2A skill by ID with the given message."""
