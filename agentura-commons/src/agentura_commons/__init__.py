"""Shared MCP + A2A base classes for Semos Agentura agents."""

from .base import (
    BaseAgentService,
    FileAttachment,
    NamedFile,
    SkillDef,
    ToolDef,
    ToolResult,
)
from .client import AgenturaClient, ClientToolResult
from .file_middleware import FileEntry, FileRegistry
from .mcp_client import AgentConnection, MCPHub
from .settings import CommonSettings
from .transport import create_app

__all__ = [
    "AgentConnection",
    "AgenturaClient",
    "BaseAgentService",
    "ClientToolResult",
    "CommonSettings",
    "FileAttachment",
    "FileEntry",
    "FileRegistry",
    "MCPHub",
    "NamedFile",
    "SkillDef",
    "ToolDef",
    "ToolResult",
    "create_app",
]
