"""Tests for mcp_tools.py - schema conversion and tool wrappers."""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock

import pytest

from langchain_core.utils.function_calling import convert_to_openai_tool
from mcp.types import CallToolResult, TextContent
from pydantic import BaseModel

from agentura_ui.file_registry import FileRegistry
from agentura_ui.mcp_hub import AgentConnection
from agentura_ui.mcp_tools import (
    _json_schema_to_pydantic,
    _make_mcp_tool_class,
    create_mcp_tools,
    drain_produced_files,
)


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


# _make_mcp_tool_class


class TestMakeMcpToolClass:
    def test_has_correct_name(self, digest_tool):
        hub = MagicMock()
        registry = FileRegistry()
        tool = _make_mcp_tool_class(digest_tool, hub, registry)
        assert tool.name == "digest_document"

    def test_has_class_attr_args_schema(self, digest_tool):
        """args_schema must be a class attr, not a property,
        so LangChain's bind_tools can introspect it."""
        hub = MagicMock()
        registry = FileRegistry()
        tool = _make_mcp_tool_class(digest_tool, hub, registry)
        # Must be a class (type), not an instance
        assert isinstance(tool.args_schema, type)
        assert issubclass(tool.args_schema, BaseModel)

    def test_convert_to_openai_tool_works(self, digest_tool):
        """bind_tools uses convert_to_openai_tool internally."""
        hub = MagicMock()
        registry = FileRegistry()
        tool = _make_mcp_tool_class(digest_tool, hub, registry)
        openai_tool = convert_to_openai_tool(tool)
        assert openai_tool["type"] == "function"
        fn = openai_tool["function"]
        assert fn["name"] == "digest_document"
        assert "source" in fn["parameters"]["properties"]

    @pytest.mark.asyncio
    async def test_arun_pre_processes_and_calls_hub(
        self, digest_tool,
    ):
        """CRITICAL: _arun resolves file from registry
        before calling the MCP hub."""
        registry = FileRegistry()
        registry.register(
            "test.pdf", b"PDF-BYTES",
            "application/pdf", "upload",
        )

        # Mock hub
        hub = MagicMock()
        hub.agent_for_tool.return_value = AgentConnection(
            "doc", "http://x/mcp/sse", "http://x",
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
        assert att["content"].startswith(
            "data:application/pdf;base64,"
        )
        assert "Result" in result


# drain_produced_files


class TestDrainProducedFiles:
    def test_drain_empty(self):
        # Clear any leftovers from other tests
        drain_produced_files()
        files = drain_produced_files()
        assert files == []

    def test_drain_clears(self):
        from agentura_ui.mcp_tools import _produced_files
        from agentura_ui.file_registry import FileEntry

        entry = FileEntry(
            "x.pdf", b"data", "application/pdf", 4, "tool:t",
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
        self, digest_tool, search_tool,
    ):
        hub = MagicMock()
        hub.all_tools.return_value = [digest_tool, search_tool]
        registry = FileRegistry()

        tools = create_mcp_tools(hub, registry)
        assert len(tools) == 2
        names = {t.name for t in tools}
        assert names == {"digest_document", "search_emails"}
