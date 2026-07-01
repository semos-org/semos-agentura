"""Shared MCP + A2A base classes for Semos Agentura agents."""

import mimetypes as _mimetypes

from .a2a_client import A2AAgentInfo, A2AResult, FileInfo
from .base import (
    AgentTool,
    BaseAgentService,
    FileAttachment,
    NamedFile,
    SkillDef,
    ToolResult,
    agent_tool,
)
from .client import AgenturaClient, ClientA2AResult, ClientToolResult
from .file_middleware import FileEntry, FileRegistry
from .llm_executor import ExecutorResult, LLMExecutor
from .mcp_client import AgentConnection, MCPHub
from .settings import CommonSettings
from .transport import create_app

# The OS mimetypes database is inconsistent across platforms for document types:
# Windows' registry lacks the OOXML extensions, so mimetypes.guess_type() returns
# "application/octet-stream" for .docx/.xlsx/.pptx there while Linux/macOS resolve
# them from /etc/mime.types. Register them once at import so every guess_type call
# site (mcp_server, a2a_server, llm_executor, client) resolves identically on all OSes.
for _ext, _mime in (
    (".docx", "application/vnd.openxmlformats-officedocument.wordprocessingml.document"),
    (".xlsx", "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"),
    (".pptx", "application/vnd.openxmlformats-officedocument.presentationml.presentation"),
    (".md", "text/markdown"),
):
    _mimetypes.add_type(_mime, _ext)

__all__ = [
    "A2AAgentInfo",
    "A2AResult",
    "AgentConnection",
    "AgentTool",
    "AgenturaClient",
    "BaseAgentService",
    "ClientA2AResult",
    "ClientToolResult",
    "CommonSettings",
    "ExecutorResult",
    "FileAttachment",
    "FileEntry",
    "FileInfo",
    "FileRegistry",
    "LLMExecutor",
    "MCPHub",
    "NamedFile",
    "SkillDef",
    "ToolResult",
    "agent_tool",
    "create_app",
]
