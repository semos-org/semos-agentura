"""Tests for mcp_tools.py - schema passthrough and tool wrappers."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
from agentura_ui.file_registry import FileRegistry
from agentura_ui.mcp_hub import AgentConnection
from agentura_ui.mcp_tools import (
    _make_mcp_tool_class,
    create_mcp_tools,
    drain_produced_files,
)
from langchain_core.utils.function_calling import convert_to_openai_tool
from mcp.types import CallToolResult, TextContent
from mcp.types import Tool as MCPTool

# _make_mcp_tool_class


class TestMakeMcpToolClass:
    @staticmethod
    def _hub(agent_name="document-agent"):
        hub = MagicMock()
        agent = MagicMock()
        agent.name = agent_name
        hub.agent_for_tool.return_value = agent
        return hub

    def test_args_schema_preserves_original(self):
        """args_schema must be the original MCP inputSchema dict
        (not a flattened Pydantic model). Critical for LLMs to see
        oneOf, const, enum, pattern, etc."""
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

        # args_schema must preserve the original, not flattened
        result = tool.args_schema
        assert isinstance(result, dict)
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
        """args_schema is the raw MCP inputSchema dict, passed
        straight to LangChain (which accepts a JSON schema dict)."""
        hub = self._hub()
        registry = FileRegistry()
        tool = _make_mcp_tool_class(digest_tool, hub, registry)
        assert isinstance(tool.args_schema, dict)
        assert "source" in tool.args_schema["properties"]

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
