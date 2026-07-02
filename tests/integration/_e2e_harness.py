"""Shared harness for cross-package integration tests (not shipped).

Starts agent services in background threads with mocked backends so the
suites run without Outlook COM, IMAP, live LLM endpoints, or (optionally)
pandoc/OCR. Lives under tests/ rather than in any package `src/`, so no
shipped package imports a sibling agent.
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


def _patch_email_service_mock_backend() -> None:
    """Inject a mock backend into the email service (no COM/IMAP).

    Patches create_backend before the service uses it and sets the lazy
    `_backend_instance` directly, so it works on Windows and Linux alike.
    """
    from semos.agentura.email.models import EmailMessage

    backend = MagicMock()
    backend.search_emails.return_value = [
        EmailMessage(
            uid="mock-1",
            subject="Test email",
            sender_name="Alice",
            sender="alice@example.com",
            date=datetime(2025, 1, 1, 12, 0),
            body_text="Hello from mock",
            attachments=[],
        ),
    ]
    backend.read_email.return_value = backend.search_emails.return_value[0]
    backend.list_events.return_value = []
    backend.free_slots.return_value = []
    backend.create_draft.return_value = "DRAFT-001"
    backend.draft_reply.return_value = "reply-1"
    backend.send_reply.return_value = None
    backend.mark_as_read.return_value = None
    backend.calendar = None
    backend.connect = MagicMock()

    import semos.agentura.email.backend as backend_mod

    backend_mod.create_backend = lambda *a, **k: backend

    import semos.agentura.email.service as svc_mod

    svc_mod._service._backend_instance = backend


def _make_minimal_docx(path: Path) -> Path:
    """Create a minimal valid DOCX for mock compose output."""
    doc_xml = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<w:document xmlns:w="http://schemas.openxmlformats.org'
        '/wordprocessingml/2006/main"><w:body><w:p><w:r>'
        "<w:t>Mock content</w:t></w:r></w:p></w:body>"
        "</w:document>"
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


def _patch_document_service_mock_tools() -> None:
    """Stub compose and digest in the document service (no pandoc/OCR).

    compose -> writes a minimal DOCX/HTML to output_path
    digest -> returns mock markdown
    Patches at all import levels (module attrs + service module).
    """
    import semos.agentura.document
    import semos.agentura.document.composition.compose as _compose_mod
    import semos.agentura.document.digestion.digest as _digest_mod
    from semos.agentura.document.models import ComposeResult, DigestResult

    def _mock_compose(source, output_path, format, **_kw):
        output_path = Path(output_path)
        src_text = str(source)[:100]
        ext = output_path.suffix.lower()
        if ext == ".docx":
            _make_minimal_docx(output_path)
        elif ext == ".html":
            output_path.write_text(
                f"<html><body><p>{src_text}</p></body></html>",
                encoding="utf-8",
            )
        else:
            output_path.write_text(
                f"Mock {ext}: {src_text}",
                encoding="utf-8",
            )
        return ComposeResult(output_path=output_path, format=format)

    def _mock_digest(source=None, **_kw):
        return DigestResult(
            markdown="# Mock Digest\n\nContent from mock.",
        )

    _compose_mod.compose = _mock_compose
    _digest_mod.digest = _mock_digest
    semos.agentura.document.compose = _mock_compose
    semos.agentura.document.digest = _mock_digest
    import semos.agentura.document.service as _svc_mod

    _svc_mod.compose = _mock_compose
    _svc_mod.digest = _mock_digest
    # tools.py does `from . import compose, digest`, so it holds its own module-level
    # bindings that the patches above do not reach; patch them too (this is what the
    # compose_document / digest_document tools actually call).
    import semos.agentura.document.tools as _tools_mod

    _tools_mod.compose = _mock_compose
    _tools_mod.digest = _mock_digest


def start_agent(agent_module: str, port: int, *, mock_document: bool = False):
    """Start an agent in a daemon thread with mocked backends.

    agent_module: "email_agent" or "document_agent".
    mock_document: also stub compose/digest (avoids pandoc/OCR/LLM). Leave
        False to exercise the real document pipeline (guard with pandoc).
    Returns (server, thread); stop via `server.should_exit = True`.
    """
    if agent_module == "document_agent":
        if mock_document:
            _patch_document_service_mock_tools()
        from semos.agentura.document.service import create_service_app
    elif agent_module == "email_agent":
        _patch_email_service_mock_backend()
        from semos.agentura.email.service import create_service_app
    else:
        raise ValueError(f"Unknown agent: {agent_module}")

    app = create_service_app(port=port)
    config = uvicorn.Config(
        app,
        host="127.0.0.1",
        port=port,
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
