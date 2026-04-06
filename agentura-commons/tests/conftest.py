"""Shared test fixtures for agentura-commons tests.

Provides mocked agent services for integration tests that don't
require real backends (COM, IMAP, LLM endpoints).
"""

from __future__ import annotations

import socket
import threading
import time
from datetime import datetime
from pathlib import Path
from unittest.mock import MagicMock
from zipfile import ZipFile

import uvicorn


def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _patch_email_service_mock_backend():
    """Inject a mock backend into the email-agent service.

    Reuses the same pattern as email-agent/tests/test_mcp.py.
    Must be called before create_service_app.
    """
    from email_agent.service import _service
    from email_agent.models import EmailMessage
    from email_agent.tools import ToolExecutor

    backend = MagicMock()
    backend.search_emails.return_value = [
        EmailMessage(
            uid="msg-1",
            subject="Test email",
            sender="alice@example.com",
            sender_name="Alice",
            to=["bob@example.com"],
            cc=[],
            date=datetime(2026, 3, 20, 10, 0),
            body_text="Test body",
            body_html="<p>Test body</p>",
        ),
    ]
    backend.list_events.return_value = []
    backend.free_slots.return_value = []
    backend.create_draft.return_value = "draft-1"
    backend.draft_reply.return_value = "reply-1"
    backend.send_reply.return_value = None
    backend.mark_as_read.return_value = None
    backend.calendar = None

    executor = ToolExecutor(backend)

    class _FakeWorker:
        async def execute(self, tool_name, args):
            return executor.execute(tool_name, args)

    _service._worker = _FakeWorker()
    _service._ensure_worker = lambda: _service._worker


def _make_minimal_docx(path: Path) -> Path:
    """Create a minimal valid DOCX for mock compose output."""
    doc_xml = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<w:document xmlns:w="http://schemas.openxmlformats.org'
        '/wordprocessingml/2006/main"><w:body><w:p><w:r>'
        '<w:t>Mock content</w:t></w:r></w:p></w:body>'
        '</w:document>'
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


def _patch_document_service_mock_tools():
    """Patch compose and digest in document-agent to avoid pandoc/OCR.

    compose -> writes a minimal DOCX/HTML to output_path
    digest -> returns mock markdown

    Patches at multiple levels to catch both direct imports
    and re-exports through __init__.py.
    """
    import document_agent
    import document_agent.composition.compose as _compose_mod
    import document_agent.digestion.digest as _digest_mod
    from document_agent.models import ComposeResult, DigestResult

    def _mock_compose(source, output_path, format, **_kw):
        output_path = Path(output_path)
        ext = output_path.suffix.lower()
        if ext == ".docx":
            _make_minimal_docx(output_path)
        elif ext == ".html":
            output_path.write_text(
                f"<html><body><p>{source[:100]}</p></body></html>",
                encoding="utf-8",
            )
        else:
            output_path.write_text(
                f"Mock {ext} output",
                encoding="utf-8",
            )
        return ComposeResult(output_path=output_path, format=format)

    def _mock_digest(_source, **_kw):
        return DigestResult(
            markdown="# Mock Digest\n\nContent from mock.",
        )

    # Patch at all import levels
    _compose_mod.compose = _mock_compose
    _digest_mod.digest = _mock_digest
    document_agent.compose = _mock_compose
    document_agent.digest = _mock_digest


def make_app(agent_module: str, port: int):
    """Create an agent app with mocked backends.

    email-agent: mock COM/IMAP backend
    document-agent: mock compose (no pandoc) and digest (no OCR)
    """
    if agent_module == "document_agent":
        _patch_document_service_mock_tools()
        from document_agent.service import create_service_app
    elif agent_module == "email_agent":
        _patch_email_service_mock_backend()
        from email_agent.service import create_service_app
    else:
        raise ValueError(f"Unknown agent: {agent_module}")
    return create_service_app(port=port)


def start_agent(agent_module: str, port: int):
    """Start agent in a background thread. Returns (server, thread)."""
    app = make_app(agent_module, port)
    config = uvicorn.Config(
        app, host="127.0.0.1", port=port,
        log_level="warning",
    )
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    for _ in range(50):
        time.sleep(0.1)
        if server.started:
            break
    return server, thread
