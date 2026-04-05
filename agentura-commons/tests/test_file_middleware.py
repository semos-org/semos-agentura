"""Unit tests for file_middleware.py - FileRegistry, pre/post middleware."""

from __future__ import annotations

import base64
import json

import pytest
from mcp.types import CallToolResult, TextContent
from mcp.types import Tool as MCPTool

from agentura_commons.file_middleware import (
    FileRegistry,
    _identify_file_params,
    human_size,
    post_process_tool_result,
    pre_process_tool_call,
)

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
            "test.pdf", b"content", "application/pdf", "upload",
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
            "5e922231_iter_01.png", b"img",
            "image/png", "tool:gen",
        )
        entry = registry.get("iter_01.png")
        assert entry is not None
        assert entry.filename == "5e922231_iter_01.png"

    def test_get_fuzzy_reverse_suffix(self, registry):
        registry.register(
            "report.pdf", b"pdf", "application/pdf", "upload",
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
    def test_by_known_name(self, digest_tool):
        params = _identify_file_params(digest_tool)
        assert "source" in params

    def test_by_x_file_annotation(self, fill_form_tool):
        params = _identify_file_params(fill_form_tool)
        assert "file_path" in params
        assert "data" not in params

    def test_by_description_heuristic(self):
        tool = MCPTool(
            name="test", description="test",
            inputSchema={
                "type": "object",
                "properties": {
                    "doc": {
                        "type": "string",
                        "description": (
                            "Accepts an absolute file path "
                            "or base64-encoded content."
                        ),
                    },
                },
            },
        )
        params = _identify_file_params(tool)
        assert "doc" in params

    def test_no_file_params(self, search_tool):
        assert len(_identify_file_params(search_tool)) == 0

    def test_empty_schema(self):
        tool = MCPTool(name="t", description="t", inputSchema={})
        assert _identify_file_params(tool) == set()


# pre_process_tool_call

class TestPreProcess:
    def test_resolves_to_file_attachment(self, registry, digest_tool):
        registry.register(
            "report.pdf", b"PDF-CONTENT", "application/pdf", "upload",
        )
        args = {"source": "report.pdf", "output_mode": "text"}
        processed = pre_process_tool_call(
            "digest_document", args, digest_tool, registry,
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
            "report.pdf", b"PDF-CONTENT", "application/pdf", "upload",
        )
        args = {"source": {"name": "report.pdf", "content": "report.pdf"}}
        processed = pre_process_tool_call(
            "digest_document", args, digest_tool, registry,
        )
        att = processed["source"]
        assert att["content"].startswith("data:")

    def test_missing_file_passes_through(self, registry, digest_tool):
        args = {"source": "nonexistent.pdf"}
        processed = pre_process_tool_call(
            "digest_document", args, digest_tool, registry,
        )
        assert processed["source"] == "nonexistent.pdf"

    def test_non_file_params_unchanged(self, registry, search_tool):
        args = {"query": "meeting", "limit": 10}
        processed = pre_process_tool_call(
            "search_emails", args, search_tool, registry,
        )
        assert processed == args

    def test_non_string_value_skipped(self, registry, digest_tool):
        args = {"source": 12345}
        processed = pre_process_tool_call(
            "digest_document", args, digest_tool, registry,
        )
        assert processed["source"] == 12345

    def test_list_of_attachments(self, registry, create_draft_tool):
        registry.register(
            "doc.docx", b"DOCX-BYTES",
            "application/vnd.openxmlformats-officedocument"
            ".wordprocessingml.document",
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
            "create_draft", args, create_draft_tool, registry,
        )
        atts = processed["attachments"]
        assert len(atts) == 1
        assert atts[0]["name"] == "doc.docx"
        assert atts[0]["content"].startswith("data:")
        assert processed["to"] == "test@example.com"


# post_process_tool_result

class TestPostProcess:
    @pytest.mark.asyncio
    async def test_fetches_download_url(self, registry, httpx_mock):
        httpx_mock.add_response(
            url="http://localhost:8002/files/out.pdf",
            content=b"GENERATED-PDF",
            headers={"content-type": "application/pdf"},
        )
        result = CallToolResult(
            content=[
                TextContent(
                    type="text",
                    text=json.dumps({
                        "download_url": "http://localhost:8002/files/out.pdf",
                        "filename": "out.pdf",
                    }),
                ),
            ],
        )
        text, files = await post_process_tool_result(
            "compose_document", result,
            "http://localhost:8002", registry,
        )
        assert len(files) == 1
        assert files[0].filename == "out.pdf"
        assert files[0].blob == b"GENERATED-PDF"
        assert registry.get("out.pdf") is not None
        parsed = json.loads(text)
        assert "download_url" not in parsed
        assert "produced_file" in parsed

    @pytest.mark.asyncio
    async def test_non_json_passthrough(self, registry):
        result = CallToolResult(
            content=[TextContent(type="text", text="plain text")],
        )
        text, files = await post_process_tool_result(
            "tool", result, "http://x", registry,
        )
        assert text == "plain text"
        assert files == []

    @pytest.mark.asyncio
    async def test_json_without_download_url(self, registry):
        result = CallToolResult(
            content=[
                TextContent(
                    type="text", text='{"markdown": "# Hello"}',
                ),
            ],
        )
        text, files = await post_process_tool_result(
            "digest", result, "http://x", registry,
        )
        assert files == []
        assert "Hello" in text

    @pytest.mark.asyncio
    async def test_empty_result(self, registry):
        result = CallToolResult(content=[])
        text, files = await post_process_tool_result(
            "tool", result, "http://x", registry,
        )
        assert text == ""
        assert files == []

    @pytest.mark.asyncio
    async def test_nested_attachment_download_urls(
        self, registry, httpx_mock,
    ):
        httpx_mock.add_response(
            url="http://localhost:8001/files/abc_report.pdf",
            content=b"PDF-ATTACHMENT",
            headers={"content-type": "application/pdf"},
        )
        httpx_mock.add_response(
            url="http://localhost:8001/files/def_image.png",
            content=b"PNG-ATTACHMENT",
            headers={"content-type": "image/png"},
        )
        result = CallToolResult(
            content=[
                TextContent(
                    type="text",
                    text=json.dumps({
                        "subject": "Meeting notes",
                        "body": "See attached.",
                        "attachments": [
                            {
                                "filename": "report.pdf",
                                "download_url": (
                                    "http://localhost:8001"
                                    "/files/abc_report.pdf"
                                ),
                            },
                            {
                                "filename": "image.png",
                                "download_url": (
                                    "http://localhost:8001"
                                    "/files/def_image.png"
                                ),
                            },
                        ],
                    }),
                ),
            ],
        )
        text, files = await post_process_tool_result(
            "read_email", result,
            "http://localhost:8001", registry,
        )
        assert len(files) == 2
        assert registry.get("report.pdf") is not None
        assert registry.get("image.png") is not None
        parsed = json.loads(text)
        for att in parsed["attachments"]:
            assert "download_url" not in att
            assert "registered_file" in att

    @pytest.mark.asyncio
    async def test_structuredcontent_preferred(
        self, registry, httpx_mock,
    ):
        """When structuredContent is present, use it instead of text."""
        httpx_mock.add_response(
            url="http://localhost:8002/files/out.docx",
            content=b"DOCX",
            headers={"content-type": "application/octet-stream"},
        )
        result = CallToolResult(
            content=[
                TextContent(type="text", text="raw json"),
            ],
            structuredContent={
                "download_url": "http://localhost:8002/files/out.docx",
                "filename": "report.docx",
                "mime_type": "application/vnd.openxmlformats",
                "size_bytes": 4,
            },
        )
        text, files = await post_process_tool_result(
            "compose", result,
            "http://localhost:8002", registry,
        )
        assert len(files) == 1
        assert files[0].filename == "report.docx"
        parsed = json.loads(text)
        assert "download_url" not in parsed
