"""Shared MCP + A2A base classes for Semos Agentura agents."""

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
