"""Unit tests for file_registry.py - FileRegistry, middleware, helpers."""

from __future__ import annotations

import base64
import json

from mcp.types import CallToolResult, TextContent

from agentura_ui.file_registry import (
    _identify_file_params,
    human_size,
    post_process_tool_result,
    pre_process_tool_call,
)
from agentura_ui.mcp_hub import AgentConnection


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
        assert entry.mime == "application/pdf"
        assert entry.size == 7
        assert entry.source == "upload"

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

    def test_delete_existing(self, registry):
        registry.register("f.pdf", b"data", "application/pdf", "upload")
        assert registry.delete("f.pdf") is True
        assert registry.get("f.pdf") is None

    def test_delete_missing(self, registry):
        assert registry.delete("nope.pdf") is False


# _identify_file_params


class TestIdentifyFileParams:
    def test_by_known_name(self, digest_tool):
        """'source' is in _KNOWN_FILE_PARAMS."""
        params = _identify_file_params(digest_tool)
        assert "source" in params

    def test_by_x_file_annotation(self, fill_form_tool):
        params = _identify_file_params(fill_form_tool)
        assert "file_path" in params
        assert "data" not in params

    def test_by_description_heuristic(self):
        from mcp.types import Tool as MCPTool

        tool = MCPTool(
            name="test",
            description="test",
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
        params = _identify_file_params(search_tool)
        assert len(params) == 0

    def test_empty_schema(self):
        from mcp.types import Tool as MCPTool

        tool = MCPTool(name="t", description="t", inputSchema={})
        assert _identify_file_params(tool) == set()

    def test_minimal_schema(self):
        from mcp.types import Tool as MCPTool

        tool = MCPTool(
            name="t", description="t",
            inputSchema={"type": "object"},
        )
        assert _identify_file_params(tool) == set()


# pre_process_tool_call


class TestPreProcess:
    def test_resolves_to_base64(self, registry, digest_tool):
        """CRITICAL: filename in registry => replaced with data URI."""
        registry.register(
            "report.pdf", b"PDF-CONTENT", "application/pdf",
            "upload",
        )
        args = {"source": "report.pdf", "output_mode": "text"}
        processed = pre_process_tool_call(
            "digest_document", args, digest_tool, registry,
        )
        assert processed["source"].startswith(
            "data:application/pdf;base64,"
        )
        # Decode and verify round-trip
        _, b64 = processed["source"].split(",", 1)
        assert base64.b64decode(b64) == b"PDF-CONTENT"
        # output_mode unchanged
        assert processed["output_mode"] == "text"

    def test_missing_file_passes_through(
        self, registry, digest_tool,
    ):
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


# post_process_tool_result


class TestPostProcess:
    async def test_fetches_download_url(
        self, registry, httpx_mock,
    ):
        httpx_mock.add_response(
            url="http://localhost:8002/files/out.pdf",
            content=b"GENERATED-PDF",
            headers={"content-type": "application/pdf"},
        )
        agent = AgentConnection(
            "doc", "http://localhost:8002/mcp/sse",
            "http://localhost:8002",
        )
        result = CallToolResult(
            content=[
                TextContent(
                    type="text",
                    text=json.dumps({
                        "download_url": (
                            "http://localhost:8002/files/out.pdf"
                        ),
                        "filename": "out.pdf",
                    }),
                ),
            ],
        )
        text, files = await post_process_tool_result(
            "compose_document", result, agent, registry,
        )
        assert len(files) == 1
        assert files[0].filename == "out.pdf"
        assert files[0].blob == b"GENERATED-PDF"
        assert registry.get("out.pdf") is not None
        # LLM text should NOT contain the raw URL
        parsed = json.loads(text)
        assert "download_url" not in parsed
        assert "produced_file" in parsed

    async def test_non_json_passthrough(self, registry):
        agent = AgentConnection(
            "doc", "http://x/mcp/sse", "http://x",
        )
        result = CallToolResult(
            content=[TextContent(type="text", text="plain text")],
        )
        text, files = await post_process_tool_result(
            "tool", result, agent, registry,
        )
        assert text == "plain text"
        assert files == []

    async def test_json_without_download_url(self, registry):
        agent = AgentConnection(
            "doc", "http://x/mcp/sse", "http://x",
        )
        result = CallToolResult(
            content=[
                TextContent(
                    type="text",
                    text='{"markdown": "# Hello"}',
                ),
            ],
        )
        text, files = await post_process_tool_result(
            "digest", result, agent, registry,
        )
        assert files == []
        assert "Hello" in text

    async def test_empty_result(self, registry):
        agent = AgentConnection(
            "doc", "http://x/mcp/sse", "http://x",
        )
        result = CallToolResult(content=[])
        text, files = await post_process_tool_result(
            "tool", result, agent, registry,
        )
        assert text == ""
        assert files == []


# resolve_file_references


class TestResolveFileReferences:
    """Test inline markdown file reference resolution."""

    def test_image_resolved_to_data_uri(self, registry):
        from agentura_ui.renderers import resolve_file_references

        registry.register(
            "diagram.png", b"\x89PNG-FAKE",
            "image/png", "tool:generate_diagram",
        )
        md = "## Diagram\n\n![A to B](diagram.png)"
        resolved = resolve_file_references(md, registry)

        assert "![A to B](data:image/png;base64," in resolved
        # Verify round-trip
        data_uri = resolved.split("(", 1)[1].rstrip(")")
        _, b64 = data_uri.split(",", 1)
        assert base64.b64decode(b64) == b"\x89PNG-FAKE"

    def test_link_resolved(self, registry):
        from agentura_ui.renderers import resolve_file_references

        registry.register(
            "report.pdf", b"%PDF-CONTENT",
            "application/pdf", "tool:compose",
        )
        md = "Download [the report](report.pdf) here."
        resolved = resolve_file_references(md, registry)
        assert "(data:application/pdf;base64," in resolved

    def test_url_not_replaced(self, registry):
        from agentura_ui.renderers import resolve_file_references

        md = "![img](https://example.com/pic.png)"
        assert resolve_file_references(md, registry) == md

    def test_data_uri_not_replaced(self, registry):
        from agentura_ui.renderers import resolve_file_references

        md = "![img](data:image/png;base64,abc)"
        assert resolve_file_references(md, registry) == md

    def test_unknown_file_not_replaced(self, registry):
        from agentura_ui.renderers import resolve_file_references

        md = "![img](nonexistent.png)"
        assert resolve_file_references(md, registry) == md

    def test_multiple_refs(self, registry):
        from agentura_ui.renderers import resolve_file_references

        registry.register(
            "a.png", b"A", "image/png", "tool:t",
        )
        registry.register(
            "b.png", b"B", "image/png", "tool:t",
        )
        md = "![](a.png) and ![](b.png)"
        resolved = resolve_file_references(md, registry)
        assert resolved.count("data:image/png;base64,") == 2

    def test_real_world_diagram_output(self, registry):
        """Matches the exact pattern from generate_diagram."""
        from agentura_ui.renderers import resolve_file_references

        registry.register(
            "5e922231_iter_01.png",
            b"\x89PNG diagram bytes",
            "image/png",
            "tool:generate_diagram",
        )
        md = (
            "## Diagram\n\n"
            "### Output File\n"
            "- **Filename:** `5e922231_iter_01.png`\n\n"
            "![A to B to C](5e922231_iter_01.png)"
        )
        resolved = resolve_file_references(md, registry)
        assert "data:image/png;base64," in resolved
        # The backtick filename reference should NOT be replaced
        assert "`5e922231_iter_01.png`" in resolved
