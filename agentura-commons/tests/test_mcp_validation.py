"""Tests for MCP server input validation.

The wrapper validates each tool call against its Pydantic args_schema before
dispatch, so a call omitting a required field raises instead of silently
running with defaults (e.g. a write_file call that drops content and writes
an empty file).
"""

from __future__ import annotations

from typing import Any

import pytest
from agentura_commons.base import AgentTool, BaseAgentService
from agentura_commons.mcp_server import create_mcp_server
from pydantic import BaseModel, Field, ValidationError


class _RequiredInput(BaseModel):
    uri: str = Field(..., description="Target")
    content: str = Field(..., description="Body - required")


class _RequiredTool(AgentTool):
    """Tool with required fields, like write_file."""

    name: str = "required_tool"
    description: str = "Needs uri and content"
    args_schema: type[BaseModel] = _RequiredInput

    async def _arun(self, **kwargs: Any) -> str:
        return f"wrote {len(kwargs.get('content', ''))} chars"


class _TestService(BaseAgentService):
    agent_name = "test-agent"
    agent_description = "Test"

    def __init__(self, tools):
        self._tools = tools

    def get_tools(self):
        return self._tools

    def get_skills(self):
        return []

    async def execute_skill(self, skill_id, message, **kwargs):
        return ""


class TestMcpServerValidation:
    async def _call(self, server, name, **kwargs):
        registered = server._tool_manager._tools.get(name)
        return await registered.fn(**kwargs)

    async def test_missing_required_field_is_rejected(self):
        """Omitting a required field raises instead of dispatching with
        defaults. This is what prevents silent 0-char write_file calls."""
        server = create_mcp_server(_TestService([_RequiredTool()]))
        with pytest.raises(ValidationError):
            await self._call(server, "required_tool", uri="session://x.md")

    async def test_all_required_fields_dispatches(self):
        server = create_mcp_server(_TestService([_RequiredTool()]))
        result = await self._call(server, "required_tool", uri="session://x.md", content="hello")
        assert result is not None
