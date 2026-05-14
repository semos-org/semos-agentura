"""MCP integration tests for document-agent.

Tests that all tools are reachable and functional via the MCP protocol,
using an in-memory transport (no uvicorn needed).

Tests marked @needs_llm require a configured LLM backend (DOCUMENT_AI_ENDPOINT).
Run with: pytest -m llm   to include them.
Run without: pytest        to skip them (default).
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
from agentura_commons.testing import mcp_client_for, parse_tool_result
from document_agent._utils import find_tool
from document_agent.models import DiagramResult
from document_agent.service import DocumentAgentService


def _check_llm_available() -> bool:
    """Check if an LLM endpoint is configured (env var or .env file)."""
    if os.environ.get("DOCUMENT_AI_ENDPOINT"):
        return True
    try:
        from dotenv import load_dotenv

        # Load from document-agent/.env (tests may run from workspace root)
        agent_dir = Path(__file__).resolve().parent.parent
        load_dotenv(agent_dir / ".env")
        return bool(os.environ.get("DOCUMENT_AI_ENDPOINT"))
    except Exception:
        return False


_has_llm = _check_llm_available()
needs_llm = pytest.mark.skipif(not _has_llm, reason="No LLM endpoint configured")


def _check_image_gen_available() -> bool:
    """Check if an image generation endpoint is configured."""
    if os.environ.get("IMAGE_GEN_ENDPOINT"):
        return True
    try:
        from dotenv import load_dotenv

        agent_dir = Path(__file__).resolve().parent.parent
        load_dotenv(agent_dir / ".env")
        return bool(os.environ.get("IMAGE_GEN_ENDPOINT"))
    except Exception:
        return False


_has_image_gen = _check_image_gen_available()
needs_image_gen = pytest.mark.skipif(not _has_image_gen, reason="No IMAGE_GEN_ENDPOINT configured")


@pytest.fixture
def service(tmp_path: Path) -> DocumentAgentService:
    """Create a DocumentAgentService with output_dir pointed at tmp_path."""
    svc = DocumentAgentService()
    svc.output_dir = tmp_path
    svc.base_url = "http://localhost:8002"
    return svc


# Tool discovery


@pytest.mark.asyncio
async def test_list_tools(service):
    async with mcp_client_for(service) as client:
        result = await client.list_tools()
        names = {t.name for t in result.tools}
        assert names == {
            "digest_document",
            "compose_document",
            "generate_diagram",
            "generate_image",
            "inspect_form",
            "fill_form",
            "merge_slides",
            "get_examples",
        }


def test_generate_image_tool_file_params():
    """generate_image should declare source and mask as file params."""
    svc = DocumentAgentService()
    tool = next(t for t in svc.get_tools() if t.name == "generate_image")
    assert "source" in tool.file_params
    assert "mask" in tool.file_params


def test_generate_diagram_embeds_file_param():
    """generate_diagram should declare embeds as a file param."""
    svc = DocumentAgentService()
    tool = next(t for t in svc.get_tools() if t.name == "generate_diagram")
    assert "embeds" in tool.file_params


@pytest.mark.asyncio
async def test_tools_have_parameters(service):
    """Every tool should expose a proper JSON Schema with typed params."""
    async with mcp_client_for(service) as client:
        result = await client.list_tools()
        for tool in result.tools:
            schema = tool.inputSchema
            assert schema.get("type") == "object", f"{tool.name} has no object schema"
            props = schema.get("properties", {})
            # All tools should have at least one parameter (except get_examples)
            if tool.name != "get_examples":
                assert len(props) > 0, f"{tool.name} has no parameters"
            # No parameter should be 'kwargs' (regression: untyped signatures)
            assert "kwargs" not in props, f"{tool.name} has **kwargs leak"


@pytest.mark.asyncio
async def test_file_params_have_x_file_annotation(service):
    """File input params should have x-file: true in schema."""
    async with mcp_client_for(service) as client:
        result = await client.list_tools()
        tools_by_name = {t.name: t for t in result.tools}

        # digest_document.source should have x-file
        digest = tools_by_name["digest_document"]
        assert digest.inputSchema["properties"]["source"].get("x-file") is True

        # inspect_form.file_path should have x-file
        inspect = tools_by_name["inspect_form"]
        assert inspect.inputSchema["properties"]["file_path"].get("x-file") is True

        # fill_form.file_path should have x-file
        fill = tools_by_name["fill_form"]
        assert fill.inputSchema["properties"]["file_path"].get("x-file") is True

        # compose_document should NOT have x-file (source is markdown text)
        compose = tools_by_name["compose_document"]
        assert compose.inputSchema["properties"]["source"].get("x-file") is None


# inspect_form via MCP


@pytest.mark.asyncio
async def test_inspect_form(service, sample_docx):
    async with mcp_client_for(service) as client:
        result = await client.call_tool("inspect_form", {"file_path": str(sample_docx)})
        data = parse_tool_result(result)
        assert isinstance(data, list)
        assert len(data) > 0
        names = {f["name"] for f in data}
        assert "StartDate" in names
        assert "FullName" in names


# fill_form via MCP


@pytest.mark.asyncio
async def test_fill_form(service, sample_docx):
    async with mcp_client_for(service) as client:
        result = await client.call_tool(
            "fill_form",
            {
                "file_path": str(sample_docx),
                "data": json.dumps({"FullName": "Test User"}),
            },
        )
        data = parse_tool_result(result)
        assert "download_url" in data
        assert data["download_url"].startswith("http://")
        assert data["filename"].endswith(".docx")
        assert data["mime_type"] == "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
        assert isinstance(data["size_bytes"], int)
        assert data["size_bytes"] > 0


# compose_document via MCP

_needs_pandoc = pytest.mark.skipif(
    not find_tool("pandoc"),
    reason="pandoc not installed",
)
_integration = pytest.mark.integration


@_integration
@_needs_pandoc
@pytest.mark.asyncio
async def test_compose_docx(service):
    """Compose raw Markdown into a DOCX via MCP."""
    async with mcp_client_for(service) as client:
        result = await client.call_tool(
            "compose_document",
            {
                "source": "# Hello\n\nThis is a test document.",
                "format": "docx",
            },
        )
        data = parse_tool_result(result)
        assert "download_url" in data
        assert data["filename"].endswith(".docx")
        assert data["mime_type"] is not None
        assert isinstance(data["size_bytes"], int) and data["size_bytes"] > 0
        # download_url contains the actual on-disk filename (UUID-prefixed)
        disk_name = data["download_url"].rsplit("/", 1)[-1]
        assert (service.output_dir / disk_name).exists()


@_integration
@_needs_pandoc
@pytest.mark.asyncio
async def test_compose_with_explicit_filename(service):
    async with mcp_client_for(service) as client:
        result = await client.call_tool(
            "compose_document",
            {
                "source": "# Report\n\nContent here.",
                "format": "docx",
                "filename": "my_report.docx",
            },
        )
        data = parse_tool_result(result)
        assert "my_report.docx" in data["filename"]


# generate_diagram via MCP


@pytest.mark.asyncio
async def test_generate_diagram_mocked(service):
    """Test generate_diagram tool via MCP with mocked LLM backend."""
    fake_img = service.output_dir / "diagram.png"
    fake_img.write_bytes(b"\x89PNG fake")

    mock_result = DiagramResult(
        code="graph TD; A-->B;",
        image_path=fake_img,
        iterations=1,
    )

    with patch("document_agent.service.generate_diagram", new_callable=AsyncMock, return_value=mock_result):
        async with mcp_client_for(service) as client:
            result = await client.call_tool(
                "generate_diagram",
                {"description": "A simple flowchart", "diagram_type": "mermaid"},
            )
            data = parse_tool_result(result)
            assert "download_url" in data
            assert data["filename"].endswith(".png")
            assert data["iterations"] == 1


@_integration
@needs_llm
@pytest.mark.asyncio
async def test_generate_diagram_real(service):
    """Test generate_diagram with a real LLM backend."""
    async with mcp_client_for(service) as client:
        result = await client.call_tool(
            "generate_diagram",
            {"description": "A simple flowchart with 3 steps: Start, Process, End"},
        )
        data = parse_tool_result(result)
        assert "download_url" in data
        assert data["iterations"] >= 1


# digest_document via MCP


@_integration
@needs_llm
@pytest.mark.asyncio
async def test_digest_document_real(service, sample_png):
    """Test digest_document with a real LLM backend."""
    async with mcp_client_for(service) as client:
        result = await client.call_tool(
            "digest_document",
            {"source": str(sample_png)},
        )
        data = parse_tool_result(result)
        assert "markdown" in data
        assert isinstance(data["markdown"], str)


# compose_document format=html (no external tools needed)


@_integration
@_needs_pandoc
@pytest.mark.asyncio
async def test_compose_html(service):
    """HTML compose should work without any external tools."""
    async with mcp_client_for(service) as client:
        result = await client.call_tool(
            "compose_document",
            {
                "source": "# Test\n\nParagraph.",
                "format": "html",
            },
        )
        data = parse_tool_result(result)
        assert "download_url" in data
        assert data["filename"].endswith(".html")


# error handling


@pytest.mark.asyncio
async def test_inspect_form_nonexistent_file(service):
    """Tool should return an error, not crash the server."""
    async with mcp_client_for(service) as client:
        result = await client.call_tool("inspect_form", {"file_path": "/nonexistent/file.docx"})
        # Should return an error in the content, not raise
        assert result.content
        text = result.content[0].text
        assert "error" in text.lower() or "Error" in text or "not found" in text.lower() or "No such file" in text


@pytest.mark.asyncio
async def test_compose_missing_format(service):
    """Missing required param should give a validation error."""
    async with mcp_client_for(service) as client:
        result = await client.call_tool(
            "compose_document",
            {"source": "# Hello"},  # missing 'format'
        )
        assert result.content
        text = result.content[0].text
        assert "error" in text.lower() or "format" in text.lower() or "required" in text.lower()


# generate_image integration tests


@_integration
@needs_image_gen
@pytest.mark.asyncio
async def test_generate_image_real(service):
    """Test generate_image with a real image generation backend."""
    async with mcp_client_for(service) as client:
        result = await client.call_tool(
            "generate_image",
            {
                "description": "A simple flat icon of a blue cloud, white background, minimal style",
                "mode": "generate",
                "size": "1024x1024",
            },
        )
        data = parse_tool_result(result)
        assert "download_url" in data
        assert data["mode"] == "generate"
        assert data["size"][0] > 0
        assert data["size"][1] > 0


@_integration
@needs_image_gen
@pytest.mark.asyncio
async def test_generate_image_edit_real(service, tmp_path):
    """Test generate_image edit mode with a real backend."""
    import io

    from PIL import Image

    # Create a source image
    img = Image.new("RGB", (1024, 1024), (255, 0, 0))
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    source_path = tmp_path / "red_square.png"
    source_path.write_bytes(buf.getvalue())

    async with mcp_client_for(service) as client:
        result = await client.call_tool(
            "generate_image",
            {
                "description": "Change the color to blue",
                "mode": "edit",
                "source": str(source_path),
            },
        )
        data = parse_tool_result(result)
        assert "download_url" in data
        assert data["mode"] == "edit"
