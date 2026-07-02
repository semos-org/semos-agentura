"""Unit tests for file_middleware.py - FileRegistry, pre/post middleware."""

from __future__ import annotations

import base64
import json

import pytest
from mcp.types import CallToolResult, TextContent
from mcp.types import Tool as MCPTool
from semos.agentura.core.file_middleware import (
    FileRegistry,
    _identify_file_params,
    human_size,
    post_process_tool_result,
    pre_process_tool_call,
)
from semos.agentura.core.mcp_client import AgentConnection

# Shared schema definitions

_FILE_ATTACHMENT_DEF = {
    "description": "A file reference with name and content.",
    "properties": {
        "name": {"title": "Name", "type": "string"},
        "content": {"title": "Content", "type": "string"},
    },
    "required": ["name", "content"],
    "title": "FileAttachment",
    "type": "object",
}


# Fixtures


@pytest.fixture
def registry():
    return FileRegistry()


@pytest.fixture
def digest_tool():
    return MCPTool(
        name="digest_document",
        description="Digest document via OCR",
        inputSchema={
            "$defs": {"FileAttachment": _FILE_ATTACHMENT_DEF},
            "type": "object",
            "properties": {
                "source": {
                    "title": "Source",
                    "x-file": True,
                    "anyOf": [
                        {"$ref": "#/$defs/FileAttachment"},
                        {"type": "string"},
                    ],
                },
                "output_mode": {
                    "default": "text",
                    "title": "Output Mode",
                    "type": "string",
                },
            },
            "required": ["source"],
        },
    )


@pytest.fixture
def fill_form_tool():
    return MCPTool(
        name="fill_form",
        description="Fill form fields",
        inputSchema={
            "$defs": {"FileAttachment": _FILE_ATTACHMENT_DEF},
            "type": "object",
            "properties": {
                "file_path": {
                    "x-file": True,
                    "anyOf": [
                        {"$ref": "#/$defs/FileAttachment"},
                        {"type": "string"},
                    ],
                },
                "data": {"type": "string"},
            },
            "required": ["file_path", "data"],
        },
    )


@pytest.fixture
def search_tool():
    return MCPTool(
        name="search_emails",
        description="Search emails",
        inputSchema={
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "limit": {"type": "integer", "default": 20},
            },
            "required": ["query"],
        },
    )


@pytest.fixture
def create_draft_tool():
    return MCPTool(
        name="create_draft",
        description="Create email draft",
        inputSchema={
            "$defs": {"FileAttachment": _FILE_ATTACHMENT_DEF},
            "type": "object",
            "properties": {
                "to": {"type": "string"},
                "subject": {"type": "string"},
                "body": {"type": "string"},
                "attachments": {
                    "x-file": True,
                    "anyOf": [
                        {
                            "type": "array",
                            "items": {
                                "$ref": "#/$defs/FileAttachment",
                            },
                        },
                        {"type": "null"},
                    ],
                    "default": None,
                },
            },
            "required": ["to", "subject", "body"],
        },
    )


# human_size


class TestHumanSize:
    def test_bytes(self):
        assert human_size(0) == "0 B"
        assert human_size(512) == "512 B"

    def test_kilobytes(self):
        assert human_size(1024) == "1.0 KB"
        assert human_size(1536) == "1.5 KB"

    def test_megabytes(self):
        assert human_size(1024 * 1024) == "1.0 MB"

    def test_gigabytes(self):
        assert human_size(1024**3) == "1.0 GB"


# FileRegistry


class TestFileRegistry:
    def test_register_and_get(self, registry):
        entry = registry.register(
            "test.pdf",
            b"content",
            "application/pdf",
            "upload",
        )
        assert entry.filename == "test.pdf"
        assert entry.blob == b"content"
        assert entry.size == 7
        got = registry.get("test.pdf")
        assert got is entry

    def test_get_missing_returns_none(self, registry):
        assert registry.get("nonexistent.pdf") is None

    def test_count_increments(self, registry):
        assert registry.count == 0
        registry.register("a.pdf", b"a", "application/pdf", "upload")
        assert registry.count == 1
        registry.register("b.pdf", b"b", "application/pdf", "upload")
        assert registry.count == 2

    def test_overwrite_same_filename(self, registry):
        registry.register("f.pdf", b"old", "application/pdf", "upload")
        registry.register("f.pdf", b"new", "application/pdf", "upload")
        assert registry.get("f.pdf").blob == b"new"

    def test_get_fuzzy_suffix_match(self, registry):
        registry.register(
            "5e922231_iter_01.png",
            b"img",
            "image/png",
            "tool:gen",
        )
        entry = registry.get("iter_01.png")
        assert entry is not None
        assert entry.filename == "5e922231_iter_01.png"

    def test_get_fuzzy_reverse_suffix(self, registry):
        registry.register(
            "report.pdf",
            b"pdf",
            "application/pdf",
            "upload",
        )
        entry = registry.get("abc_report.pdf")
        assert entry is not None

    def test_get_fuzzy_no_match(self, registry):
        registry.register("foo.pdf", b"x", "application/pdf", "upload")
        assert registry.get("bar.pdf") is None

    def test_delete_existing(self, registry):
        registry.register("f.pdf", b"data", "application/pdf", "upload")
        assert registry.delete("f.pdf") is True
        assert registry.get("f.pdf") is None

    def test_delete_missing(self, registry):
        assert registry.delete("nope.pdf") is False


# _identify_file_params


class TestIdentifyFileParams:
    def test_by_x_file_on_source(self, digest_tool):
        """source has x-file: true in schema."""
        params = _identify_file_params(digest_tool)
        assert "source" in params

    def test_by_x_file_annotation(self, fill_form_tool):
        params = _identify_file_params(fill_form_tool)
        assert "file_path" in params
        assert "data" not in params

    def test_no_heuristic_without_x_file(self):
        """Params named 'source' are NOT detected without x-file annotation."""
        tool = MCPTool(
            name="test",
            description="test",
            inputSchema={
                "type": "object",
                "properties": {
                    "source": {"type": "string", "description": "Markdown text or file path"},
                },
            },
        )
        params = _identify_file_params(tool)
        assert "source" not in params

    def test_no_file_params(self, search_tool):
        assert len(_identify_file_params(search_tool)) == 0

    def test_empty_schema(self):
        tool = MCPTool(name="t", description="t", inputSchema={})
        assert _identify_file_params(tool) == set()


# pre_process_tool_call


class TestPreProcess:
    def test_resolves_to_file_attachment(self, registry, digest_tool):
        registry.register(
            "report.pdf",
            b"PDF-CONTENT",
            "application/pdf",
            "upload",
        )
        args = {"source": "report.pdf", "output_mode": "text"}
        processed = pre_process_tool_call(
            "digest_document",
            args,
            digest_tool,
            registry,
        )
        att = processed["source"]
        assert isinstance(att, dict)
        assert att["name"] == "report.pdf"
        assert att["content"].startswith("data:application/pdf;base64,")
        _, b64 = att["content"].split(",", 1)
        assert base64.b64decode(b64) == b"PDF-CONTENT"
        assert processed["output_mode"] == "text"

    def test_resolves_dict_value(self, registry, digest_tool):
        registry.register(
            "report.pdf",
            b"PDF-CONTENT",
            "application/pdf",
            "upload",
        )
        args = {"source": {"name": "report.pdf", "content": "report.pdf"}}
        processed = pre_process_tool_call(
            "digest_document",
            args,
            digest_tool,
            registry,
        )
        att = processed["source"]
        assert att["content"].startswith("data:")

    def test_missing_file_raises_error(self, registry, digest_tool):
        from semos.agentura.core.file_middleware import FileNotResolvedError

        args = {"source": "nonexistent.pdf"}
        with pytest.raises(FileNotResolvedError, match="nonexistent.pdf"):
            pre_process_tool_call(
                "digest_document",
                args,
                digest_tool,
                registry,
            )

    def test_non_file_params_unchanged(self, registry, search_tool):
        args = {"query": "meeting", "limit": 10}
        processed = pre_process_tool_call(
            "search_emails",
            args,
            search_tool,
            registry,
        )
        assert processed == args

    def test_non_string_value_skipped(self, registry, digest_tool):
        args = {"source": 12345}
        processed = pre_process_tool_call(
            "digest_document",
            args,
            digest_tool,
            registry,
        )
        assert processed["source"] == 12345

    def test_list_of_attachments(self, registry, create_draft_tool):
        registry.register(
            "doc.docx",
            b"DOCX-BYTES",
            "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            "tool:compose_document",
        )
        args = {
            "to": "test@example.com",
            "subject": "Test",
            "body": "See attached.",
            "attachments": [
                {"name": "doc.docx", "content": "doc.docx (228.3 KB)"},
            ],
        }
        processed = pre_process_tool_call(
            "create_draft",
            args,
            create_draft_tool,
            registry,
        )
        atts = processed["attachments"]
        assert len(atts) == 1
        assert atts[0]["name"] == "doc.docx"
        assert atts[0]["content"].startswith("data:")
        assert processed["to"] == "test@example.com"

    def test_nested_x_file_in_array_items(self, registry):
        """Resolve x-file fields inside array-of-objects (e.g., embeds)."""
        registry.register("logo.png", b"PNG-DATA", "image/png", "upload")
        # Schema with EmbedItem that has x-file on 'content'
        tool = MCPTool(
            name="generate_diagram",
            description="Generate diagram",
            inputSchema={
                "type": "object",
                "properties": {
                    "description": {"type": "string"},
                    "embeds": {
                        "anyOf": [
                            {"type": "array", "items": {"$ref": "#/$defs/EmbedItem"}},
                            {"type": "null"},
                        ],
                        "default": None,
                    },
                },
                "$defs": {
                    "EmbedItem": {
                        "type": "object",
                        "properties": {
                            "name": {"type": "string"},
                            "content": {"type": "string", "x-file": True},
                            "description": {"type": "string"},
                        },
                        "required": ["name"],
                    },
                },
            },
        )
        args = {
            "description": "A diagram with a logo",
            "embeds": [{"name": "logo.png", "content": "", "description": "center"}],
        }
        processed = pre_process_tool_call("generate_diagram", args, tool, registry)
        embeds = processed["embeds"]
        assert len(embeds) == 1
        assert embeds[0]["name"] == "logo.png"
        assert embeds[0]["content"].startswith("data:image/png;base64,")
        assert embeds[0]["description"] == "center"

    def test_string_to_list_normalization(self, registry):
        """LLM passes string instead of list for array param."""
        registry.register("img.png", b"IMG", "image/png", "upload")
        tool = MCPTool(
            name="generate_diagram",
            description="Generate diagram",
            inputSchema={
                "type": "object",
                "properties": {
                    "embeds": {
                        "anyOf": [
                            {"type": "array", "items": {"$ref": "#/$defs/EmbedItem"}},
                            {"type": "null"},
                        ],
                        "default": None,
                    },
                },
                "$defs": {
                    "EmbedItem": {
                        "type": "object",
                        "properties": {
                            "name": {"type": "string"},
                            "content": {"type": "string", "x-file": True},
                        },
                        "required": ["name"],
                    },
                },
            },
        )
        # LLM passes plain string instead of list
        args = {"embeds": "img.png"}
        processed = pre_process_tool_call("generate_diagram", args, tool, registry)
        embeds = processed["embeds"]
        assert isinstance(embeds, list)
        assert len(embeds) == 1


# post_process_tool_result


class TestPostProcess:
    @pytest.mark.asyncio
    async def test_fetches_resource_link(self, registry, httpx_mock):
        """ResourceLink in content is fetched and registered."""
        httpx_mock.add_response(
            url="http://localhost:8002/files/out.pdf",
            content=b"GENERATED-PDF",
            headers={"content-type": "application/pdf"},
        )
        from mcp.types import ResourceLink

        result = CallToolResult(
            content=[
                TextContent(type="text", text='{"iterations": 1}'),
                ResourceLink(
                    type="resource_link",
                    uri="http://localhost:8002/files/out.pdf",
                    name="out.pdf",
                    mimeType="application/pdf",
                ),
            ],
        )
        agent = AgentConnection(
            "doc",
            "http://localhost:8002/mcp/sse",
            "http://localhost:8002",
        )
        text, files = await post_process_tool_result(
            "compose_document",
            result,
            agent,
            registry,
        )
        assert len(files) == 1
        assert files[0].filename == "out.pdf"
        assert files[0].blob == b"GENERATED-PDF"
        assert registry.get("out.pdf") is not None
        parsed = json.loads(text)
        assert "produced_file" in parsed

    @pytest.mark.asyncio
    async def test_multiple_resource_links(self, registry, httpx_mock):
        """Multiple ResourceLinks are all fetched."""
        httpx_mock.add_response(
            url="http://localhost:8002/files/a.pdf",
            content=b"PDF-A",
            headers={"content-type": "application/pdf"},
        )
        httpx_mock.add_response(
            url="http://localhost:8002/files/b.png",
            content=b"PNG-B",
            headers={"content-type": "image/png"},
        )
        from mcp.types import ResourceLink

        result = CallToolResult(
            content=[
                TextContent(type="text", text='{"info": "two files"}'),
                ResourceLink(
                    type="resource_link",
                    uri="http://localhost:8002/files/a.pdf",
                    name="a.pdf",
                ),
                ResourceLink(
                    type="resource_link",
                    uri="http://localhost:8002/files/b.png",
                    name="b.png",
                ),
            ],
        )
        agent = AgentConnection(
            "doc",
            "http://localhost:8002/mcp/sse",
            "http://localhost:8002",
        )
        text, files = await post_process_tool_result(
            "tool",
            result,
            agent,
            registry,
        )
        assert len(files) == 2
        assert registry.get("a.pdf") is not None
        assert registry.get("b.png") is not None
        parsed = json.loads(text)
        assert "produced_files" in parsed
        assert len(parsed["produced_files"]) == 2

    @pytest.mark.asyncio
    async def test_non_json_passthrough(self, registry):
        result = CallToolResult(
            content=[TextContent(type="text", text="plain text")],
        )
        agent = AgentConnection("x", "http://x/mcp/sse", "http://x")
        text, files = await post_process_tool_result(
            "tool",
            result,
            agent,
            registry,
        )
        assert text == "plain text"
        assert files == []

    @pytest.mark.asyncio
    async def test_json_without_files(self, registry):
        result = CallToolResult(
            content=[
                TextContent(
                    type="text",
                    text='{"markdown": "# Hello"}',
                ),
            ],
        )
        agent = AgentConnection("x", "http://x/mcp/sse", "http://x")
        text, files = await post_process_tool_result(
            "digest",
            result,
            agent,
            registry,
        )
        assert files == []
        assert "Hello" in text

    @pytest.mark.asyncio
    async def test_empty_result(self, registry):
        agent = AgentConnection("x", "http://x/mcp/sse", "http://x")
        result = CallToolResult(content=[])
        text, files = await post_process_tool_result(
            "tool",
            result,
            agent,
            registry,
        )
        assert text == ""
        assert files == []

    @pytest.mark.asyncio
    async def test_structuredcontent_metadata(
        self,
        registry,
        httpx_mock,
    ):
        """structuredContent metadata cleaned when ResourceLinks present."""
        httpx_mock.add_response(
            url="http://localhost:8002/files/out.docx",
            content=b"DOCX",
            headers={"content-type": "application/octet-stream"},
        )
        from mcp.types import ResourceLink

        result = CallToolResult(
            content=[
                TextContent(type="text", text="{}"),
                ResourceLink(
                    type="resource_link",
                    uri="http://localhost:8002/files/out.docx",
                    name="report.docx",
                ),
            ],
            structuredContent={
                "filename": "report.docx",
                "iterations": 1,
            },
        )
        agent = AgentConnection(
            "doc",
            "http://localhost:8002/mcp/sse",
            "http://localhost:8002",
        )
        text, files = await post_process_tool_result(
            "compose",
            result,
            agent,
            registry,
        )
        assert len(files) == 1
        assert files[0].filename == "report.docx"
        parsed = json.loads(text)
        assert "produced_file" in parsed
        assert "iterations" in parsed
