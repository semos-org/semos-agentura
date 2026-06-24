"""Tests for MCP server schema handling."""

from __future__ import annotations

from typing import Any

from agentura_commons.base import AgentTool, BaseAgentService
from agentura_commons.mcp_server import create_mcp_server
from pydantic import BaseModel, Field


class _DummyInput(BaseModel):
    name: str = Field(default="", description="Name")


class _FlatTool(AgentTool):
    """Tool with a standard flat schema (properties at top level)."""

    name: str = "flat_tool"
    description: str = "A flat tool"
    args_schema: type[BaseModel] = _DummyInput

    async def _arun(self, **kwargs: Any) -> str:
        return "ok"


class _OneOfTool(AgentTool):
    """Tool with a oneOf discriminated union schema (no top-level properties)."""

    name: str = "oneof_tool"
    description: str = "A nested-oneOf tool"
    args_schema: type[BaseModel] = _DummyInput
    # Mirrors the real add_root schema: top level is a plain object (Anthropic
    # forbids top-level oneOf), with a discriminated union nested under a
    # property. This guards that the server preserves nested combinators.
    parameters_override: dict[str, Any] | None = {
        "type": "object",
        "required": ["name", "protocol"],
        "properties": {
            "name": {"type": "string"},
            "protocol": {"type": "string", "enum": ["local", "google_drive"]},
            "kwargs": {
                "description": "Protocol-specific options.",
                "oneOf": [
                    {"title": "local", "type": "object"},
                    {
                        "title": "google_drive",
                        "type": "object",
                        "additionalProperties": False,
                        "properties": {"share_url": {"type": "string"}},
                        "required": ["share_url"],
                    },
                ],
            },
        },
    }

    async def _arun(self, **kwargs: Any) -> str:
        return "ok"


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


class TestMcpServerSchemaReplacement:
    def test_flat_schema_is_applied(self):
        """Standard tool schema with top-level properties is applied."""
        tool = _FlatTool()
        service = _TestService([tool])
        server = create_mcp_server(service)

        registered = server._tool_manager._tools.get("flat_tool")
        assert registered is not None
        params = registered.parameters
        assert "properties" in params
        assert "name" in params["properties"]

    def test_oneof_schema_is_applied(self):
        """A rich schema with a nested discriminated union (oneOf under a
        property) must be applied verbatim. This guards the bug where the
        server flattened the schema and the LLM lost the protocol-specific
        kwargs."""
        tool = _OneOfTool()
        service = _TestService([tool])
        server = create_mcp_server(service)

        registered = server._tool_manager._tools.get("oneof_tool")
        assert registered is not None
        params = registered.parameters

        # Top level is a plain object (no combinators) - Anthropic-compatible
        assert not (set(params) & {"oneOf", "allOf", "anyOf"})
        assert "properties" in params

        # Nested oneOf under kwargs is preserved with titles + constraints
        kwargs = params["properties"]["kwargs"]
        assert "oneOf" in kwargs
        titles = [v["title"] for v in kwargs["oneOf"]]
        assert titles == ["local", "google_drive"]

        gd = [v for v in kwargs["oneOf"] if v["title"] == "google_drive"][0]
        assert gd["additionalProperties"] is False
        assert "share_url" in gd["properties"]
