"""Shared fixtures for agentura-ui tests."""

from __future__ import annotations

import panel as pn
import pytest
from mcp.types import Tool as MCPTool

from agentura_ui.file_registry import FileRegistry
from agentura_ui.mcp_hub import AgentConnection

PORT = [6100]


@pytest.fixture
def port():
    PORT[0] += 1
    return PORT[0]


@pytest.fixture(autouse=True)
def server_cleanup():
    """Clean up Panel server state after each test."""
    try:
        yield
    finally:
        pn.state.reset()


@pytest.fixture
def registry():
    return FileRegistry()


@pytest.fixture
def sample_pdf_bytes():
    """Minimal PDF-like bytes for testing."""
    return b"%PDF-1.4 fake content for testing"


@pytest.fixture
def sample_pdf(tmp_path, sample_pdf_bytes):
    """Write a sample PDF to disk."""
    p = tmp_path / "test.pdf"
    p.write_bytes(sample_pdf_bytes)
    return p


@pytest.fixture
def digest_tool():
    """MCP Tool matching document-agent's digest_document."""
    return MCPTool(
        name="digest_document",
        description="Digest document via OCR",
        inputSchema={
            "type": "object",
            "properties": {
                "source": {
                    "title": "Source",
                    "type": "string",
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
    """MCP Tool with x-file annotation."""
    return MCPTool(
        name="fill_form",
        description="Fill form fields",
        inputSchema={
            "type": "object",
            "properties": {
                "file_path": {
                    "type": "string",
                    "x-file": True,
                    "description": "PDF file to fill.",
                },
                "data": {
                    "type": "string",
                    "description": "JSON field data.",
                },
            },
            "required": ["file_path", "data"],
        },
    )


@pytest.fixture
def search_tool():
    """MCP Tool with no file params (email search)."""
    return MCPTool(
        name="search_emails",
        description="Search emails",
        inputSchema={
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "Search query.",
                },
                "limit": {
                    "type": "integer",
                    "default": 20,
                },
            },
            "required": ["query"],
        },
    )


@pytest.fixture
def two_agents():
    """Two AgentConnection objects for testing MCPHub."""
    return [
        AgentConnection(
            name="email-agent",
            url="http://localhost:8001/mcp/sse",
            base_url="http://localhost:8001",
        ),
        AgentConnection(
            name="document-agent",
            url="http://localhost:8002/mcp/sse",
            base_url="http://localhost:8002",
        ),
    ]
