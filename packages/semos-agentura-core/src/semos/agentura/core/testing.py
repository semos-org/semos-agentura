"""Shared test helpers for agent integration tests.

Importable from semos.agentura.core.testing (not conftest).
"""

from __future__ import annotations

import socket
from pathlib import Path
from zipfile import ZipFile


def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def mcp_client_for(service):
    """Create an in-memory MCP client session for a BaseAgentService.

    Returns an async context manager that yields a ClientSession.
    No HTTP or uvicorn needed - uses in-memory streams.

    Usage:
        async with mcp_client_for(service) as client:
            result = await client.list_tools()
    """
    from contextlib import asynccontextmanager

    from mcp.shared.memory import (
        create_connected_server_and_client_session,
    )

    from .mcp_server import create_mcp_server

    @asynccontextmanager
    async def _ctx():
        server = create_mcp_server(service)
        async with create_connected_server_and_client_session(server) as client_session:
            yield client_session

    return _ctx()


def parse_tool_result(result) -> dict | str:
    """Parse a CallToolResult into a dict (if JSON) or plain string.

    Prefers structuredContent if available, falls back to text content.
    """
    import json as _json

    # Error results: always return text
    if getattr(result, "isError", False):
        texts = []
        for block in result.content or []:
            if hasattr(block, "text"):
                texts.append(block.text)
        return "\n".join(texts)

    # structuredContent (new MCP spec)
    sc = getattr(result, "structuredContent", None)
    if sc:
        data = dict(sc) if not isinstance(sc, dict) else sc
        # Unwrap single-key wrapper: {"result": [...]} -> [...]
        # but keep {"error": [...]} as-is
        if len(data) == 1:
            key = next(iter(data))
            val = data[key]
            if isinstance(val, list) and key != "error":
                return val
        return data

    # Text content blocks
    texts = []
    for block in result.content or []:
        if hasattr(block, "text"):
            texts.append(block.text)
    text = "\n".join(texts)
    try:
        return _json.loads(text)
    except (ValueError, TypeError):
        return text


def make_sample_docx(path: Path) -> Path:
    """Create a minimal DOCX with a FullName content control."""
    doc_xml = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<w:document xmlns:w="http://schemas.openxmlformats.org'
        '/wordprocessingml/2006/main"><w:body><w:p><w:sdt>'
        '<w:sdtPr><w:alias w:val="FullName"/></w:sdtPr>'
        "<w:sdtContent><w:p><w:r><w:t></w:t></w:r></w:p>"
        "</w:sdtContent></w:sdt></w:p></w:body></w:document>"
    )
    rels = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Relationships xmlns="http://schemas.openxmlformats.org'
        '/package/2006/relationships">'
        '<Relationship Id="rId1" Type="http://schemas.openxmlformats'
        '.org/officeDocument/2006/relationships/officeDocument"'
        ' Target="word/document.xml"/></Relationships>'
    )
    ct = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Types xmlns="http://schemas.openxmlformats.org'
        '/package/2006/content-types">'
        '<Default Extension="rels" ContentType="application/'
        'vnd.openxmlformats-package.relationships+xml"/>'
        '<Default Extension="xml" ContentType="application/xml"/>'
        '<Override PartName="/word/document.xml" ContentType='
        '"application/vnd.openxmlformats-officedocument'
        '.wordprocessingml.document.main+xml"/></Types>'
    )
    with ZipFile(path, "w") as zf:
        zf.writestr("[Content_Types].xml", ct)
        zf.writestr("_rels/.rels", rels)
        zf.writestr("word/document.xml", doc_xml)
    return path
