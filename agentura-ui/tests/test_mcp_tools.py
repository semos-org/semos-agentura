"""Tests for mcp_tools.py - schema conversion and tool wrappers."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
from agentura_ui.file_registry import FileRegistry
from agentura_ui.mcp_hub import AgentConnection
from agentura_ui.mcp_tools import (
    _json_schema_to_pydantic,
    _make_mcp_tool_class,
    create_mcp_tools,
    drain_produced_files,
)
from langchain_core.utils.function_calling import convert_to_openai_tool
from mcp.types import CallToolResult, TextContent
from mcp.types import Tool as MCPTool
from pydantic import BaseModel

# _json_schema_to_pydantic


class TestJsonSchemaToPydantic:
    def test_required_string_field(self):
        schema = {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "Search query",
                },
            },
            "required": ["query"],
        }
        Model = _json_schema_to_pydantic(schema, "Test")
        assert issubclass(Model, BaseModel)
        js = Model.model_json_schema()
        assert "query" in js["required"]

    def test_optional_with_default(self):
        schema = {
            "type": "object",
            "properties": {
                "limit": {
                    "type": "integer",
                    "default": 20,
                    "description": "Max results",
                },
            },
        }
        Model = _json_schema_to_pydantic(schema, "Test")
        instance = Model()
        assert instance.limit == 20

    def test_all_types(self):
        schema = {
            "type": "object",
            "properties": {
                "s": {"type": "string"},
                "i": {"type": "integer"},
                "f": {"type": "number"},
                "b": {"type": "boolean"},
                "o": {"type": "object"},
                "a": {"type": "array"},
            },
        }
        Model = _json_schema_to_pydantic(schema, "AllTypes")
        assert issubclass(Model, BaseModel)

    def test_empty_schema(self):
        Model = _json_schema_to_pydantic({}, "Empty")
        instance = Model()
        assert instance is not None

    def test_oneof_property_accepts_object(self):
        """A property using oneOf (no 'type') must accept a structured dict,
        not be coerced to str. Regression for add_root's config param."""
        schema = {
            "type": "object",
            "properties": {
                "name": {"type": "string"},
                "config": {
                    "description": "Mount config",
                    "oneOf": [
                        {"title": "local", "properties": {"protocol": {"const": "local"}}},
                    ],
                },
            },
            "required": ["name", "config"],
        }
        Model = _json_schema_to_pydantic(schema, "AddRoot")
        inst = Model.model_validate({"name": "proj", "config": {"protocol": "local", "base_path": "/x"}})
        assert inst.config == {"protocol": "local", "base_path": "/x"}

    def test_typeless_property_is_any(self):
        """A property with neither 'type' nor a combinator defaults to Any."""
        schema = {"type": "object", "properties": {"x": {"description": "no type"}}}
        Model = _json_schema_to_pydantic(schema, "T")
        assert Model.model_validate({"x": {"nested": 1}}).x == {"nested": 1}


# _make_mcp_tool_class


class TestMakeMcpToolClass:
    @staticmethod
    def _hub(agent_name="document-agent"):
        hub = MagicMock()
        agent = MagicMock()
        agent.name = agent_name
        hub.agent_for_tool.return_value = agent
        return hub

    def test_get_input_schema_preserves_original(self):
        """get_input_schema must return the original MCP inputSchema,
        not the flattened Pydantic schema. This is critical for LLMs
        to see oneOf, const, enum, pattern, etc."""
        rich_schema = {
            "type": "object",
            "required": ["name", "protocol"],
            "oneOf": [
                {
                    "title": "local",
                    "properties": {
                        "name": {"type": "string"},
                        "protocol": {"const": "local"},
                        "base_path": {"type": "string"},
                    },
                    "required": ["name", "protocol", "base_path"],
                },
                {
                    "title": "google_drive",
                    "properties": {
                        "name": {"type": "string"},
                        "protocol": {"const": "google_drive"},
                        "kwargs": {
                            "type": "object",
                            "additionalProperties": False,
                            "properties": {
                                "share_url": {
                                    "type": "string",
                                    "pattern": r"^https://(drive|docs)\.google\.com/.*$",
                                },
                            },
                            "required": ["share_url"],
                        },
                    },
                    "required": ["name", "protocol", "kwargs"],
                },
            ],
        }
        mcp_tool = MCPTool(
            name="add_root",
            description="Mount a filesystem root",
            inputSchema=rich_schema,
        )
        hub = self._hub("filesystem-agent")
        registry = FileRegistry()
        tool = _make_mcp_tool_class(mcp_tool, hub, registry)

        # mcp_input_schema must preserve the original, not flattened
        result = tool.mcp_input_schema
        assert "oneOf" in result, "oneOf lost - LLM won't see protocol options"
        assert len(result["oneOf"]) == 2
        # Check const values are preserved
        protos = [e["properties"]["protocol"]["const"] for e in result["oneOf"]]
        assert "local" in protos
        assert "google_drive" in protos
        # Check nested kwargs schema preserved
        gd = [e for e in result["oneOf"] if e["title"] == "google_drive"][0]
        assert gd["properties"]["kwargs"]["additionalProperties"] is False
        assert "share_url" in gd["properties"]["kwargs"]["properties"]

    def test_has_correct_name(self, digest_tool):
        hub = self._hub()
        registry = FileRegistry()
        tool = _make_mcp_tool_class(digest_tool, hub, registry)
        assert tool.name == "document_agent__digest_document"

    def test_has_class_attr_args_schema(self, digest_tool):
        """args_schema must be a class attr, not a property,
        so LangChain's bind_tools can introspect it."""
        hub = self._hub()
        registry = FileRegistry()
        tool = _make_mcp_tool_class(digest_tool, hub, registry)
        # Must be a class (type), not an instance
        assert isinstance(tool.args_schema, type)
        assert issubclass(tool.args_schema, BaseModel)

    def test_convert_to_openai_tool_works(self, digest_tool):
        """bind_tools uses convert_to_openai_tool internally."""
        hub = self._hub()
        registry = FileRegistry()
        tool = _make_mcp_tool_class(digest_tool, hub, registry)
        openai_tool = convert_to_openai_tool(tool)
        assert openai_tool["type"] == "function"
        fn = openai_tool["function"]
        assert fn["name"] == "document_agent__digest_document"
        assert "source" in fn["parameters"]["properties"]

    @pytest.mark.asyncio
    async def test_arun_pre_processes_and_calls_hub(
        self,
        digest_tool,
    ):
        """CRITICAL: _arun resolves file from registry
        before calling the MCP hub."""
        registry = FileRegistry()
        registry.register(
            "test.pdf",
            b"PDF-BYTES",
            "application/pdf",
            "upload",
        )

        # Mock hub
        hub = MagicMock()
        hub.agent_for_tool.return_value = AgentConnection(
            "doc",
            "http://x/mcp/sse",
            "http://x",
        )
        hub.call_tool = AsyncMock(
            return_value=CallToolResult(
                content=[
                    TextContent(
                        type="text",
                        text='{"markdown": "# Result"}',
                    ),
                ],
            ),
        )

        tool = _make_mcp_tool_class(digest_tool, hub, registry)
        result = await tool._arun(source="test.pdf")

        # Verify hub.call_tool was called with FileAttachment,
        # not raw filename
        hub.call_tool.assert_called_once()
        call_args = hub.call_tool.call_args
        processed_args = call_args[0][1]  # positional arg 1
        att = processed_args["source"]
        assert isinstance(att, dict)
        assert att["name"] == "test.pdf"
        assert att["content"].startswith("data:application/pdf;base64,")
        assert "Result" in result


# drain_produced_files


class TestDrainProducedFiles:
    def test_drain_empty(self):
        # Clear any leftovers from other tests
        drain_produced_files()
        files = drain_produced_files()
        assert files == []

    def test_drain_clears(self):
        from agentura_ui.file_registry import FileEntry
        from agentura_ui.mcp_tools import _produced_files

        entry = FileEntry(
            "x.pdf",
            b"data",
            "application/pdf",
            4,
            "tool:t",
        )
        _produced_files.append(entry)

        files = drain_produced_files()
        assert len(files) == 1
        assert files[0].filename == "x.pdf"

        # Second drain is empty
        assert drain_produced_files() == []


# create_mcp_tools


class TestCreateMcpTools:
    def test_creates_one_per_mcp_tool(
        self,
        digest_tool,
        search_tool,
    ):
        hub = MagicMock()
        hub.all_tools.return_value = [
            digest_tool,
            search_tool,
        ]
        # Each tool needs agent_for_tool to return an agent
        agent = MagicMock()
        agent.name = "test-agent"
        hub.agent_for_tool.return_value = agent
        registry = FileRegistry()

        tools = create_mcp_tools(hub, registry)
        assert len(tools) == 2
        names = {t.name for t in tools}
        assert names == {
            "test_agent__digest_document",
            "test_agent__search_emails",
        }
