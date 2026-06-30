"""Integration tests for A2A protocol against auto-started agents.

Tests explicit tool calls via A2A JSON-RPC, agent card discovery,
and file round-trips. Uses a2a-sdk 1.0 Client API.
"""

from __future__ import annotations

import shutil
from pathlib import Path
from uuid import uuid4
from zipfile import ZipFile

import httpx
import pytest
from a2a.client import Client, ClientConfig, ClientFactory
from a2a.types import (
    Message,
    Part,
    Role,
    SendMessageRequest,
)
from google.protobuf.struct_pb2 import Struct, Value
from semos.agentura.core.testing import free_port, start_agent

# compose_document/digest_document shell out to pandoc; gate those tests so they skip
# cleanly without pandoc (CI installs it).
needs_pandoc = pytest.mark.skipif(shutil.which("pandoc") is None, reason="pandoc not installed")


async def _connect(base_url: str) -> Client:
    """Connect to an A2A agent using REST (HTTP+JSON) binding."""
    config = ClientConfig(
        supported_protocol_bindings=["HTTP+JSON"],
        httpx_client=httpx.AsyncClient(timeout=60),
    )
    return await ClientFactory.connect(
        base_url,
        client_config=config,
    )


def _make_sample_docx(path: Path) -> Path:
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


def _tool_call_message(
    tool: str,
    arguments: dict,
    files: list[tuple[str, bytes, str]] | None = None,
) -> Message:
    """Build an A2A Message for an explicit tool call."""
    data_struct = Struct()
    data_struct.update({"tool": tool, "arguments": arguments})
    parts = [Part(data=Value(struct_value=data_struct))]
    for fname, blob, mime in files or []:
        parts.append(
            Part(
                raw=blob,
                filename=fname,
                media_type=mime,
            )
        )
    return Message(
        message_id=str(uuid4()),
        role=Role.ROLE_USER,
        parts=parts,
    )


async def _send_and_collect(
    client: Client,
    message: Message,
) -> tuple[str, list[dict]]:
    """Send message via A2A, collect final text and file artifacts."""
    request = SendMessageRequest(message=message)
    text_parts = []
    file_artifacts = []
    final_task = None

    # Collect artifacts from stream events AND final task
    async for stream_resp, task in client.send_message(request):
        if task:
            final_task = task
        # Collect from artifact_update stream events
        art_update = getattr(stream_resp, "artifact_update", None)
        if art_update and getattr(art_update, "artifact", None):
            art = art_update.artifact
            for p in art.parts:
                if p.HasField("url"):
                    file_artifacts.append(
                        {
                            "url": p.url,
                            "filename": p.filename,
                            "mime_type": p.media_type,
                        }
                    )

    if final_task:
        # Extract text from status message
        if final_task.status and final_task.status.message:
            msg = final_task.status.message
            for p in msg.parts:
                if p.HasField("text"):
                    text_parts.append(p.text)
        # Deduplicate: also check task.artifacts
        seen = {f["url"] for f in file_artifacts}
        for art in final_task.artifacts:
            for p in art.parts:
                if p.HasField("url") and p.url not in seen:
                    file_artifacts.append(
                        {
                            "url": p.url,
                            "filename": p.filename,
                            "mime_type": p.media_type,
                        }
                    )
                    seen.add(p.url)

    return "\n".join(text_parts), file_artifacts


# Agent Card


class TestAgentCard:
    @pytest.fixture(autouse=True)
    def _agent(self):
        self.port = free_port()
        self.base_url = f"http://127.0.0.1:{self.port}"
        self.server, self.thread = start_agent(
            "document_agent",
            self.port,
        )
        yield
        self.server.should_exit = True
        self.thread.join(timeout=5)

    @pytest.mark.asyncio
    async def test_agent_card_accessible(self):
        url = f"{self.base_url}/.well-known/agent-card.json"
        async with httpx.AsyncClient() as http:
            resp = await http.get(url, timeout=5)
        assert resp.status_code == 200
        data = resp.json()
        assert data["name"] == "Document Agent"

    @pytest.mark.asyncio
    async def test_agent_card_has_skills(self):
        url = f"{self.base_url}/.well-known/agent-card.json"
        async with httpx.AsyncClient() as http:
            resp = await http.get(url, timeout=5)
        data = resp.json()
        skill_ids = {s["id"] for s in data["skills"]}
        assert "document-processing" in skill_ids


# Tool calls via A2A


class TestA2AToolCalls:
    @pytest.fixture(autouse=True)
    def _agent(self):
        self.port = free_port()
        self.base_url = f"http://127.0.0.1:{self.port}"
        self.server, self.thread = start_agent(
            "document_agent",
            self.port,
        )
        yield
        self.server.should_exit = True
        self.thread.join(timeout=5)

    @needs_pandoc
    @pytest.mark.asyncio
    async def test_compose_html(self):
        client = await _connect(self.base_url)
        msg = _tool_call_message(
            "compose_document",
            {"source_markdown": "# A2A Test\n\nHello.", "format": "html"},
        )
        text, files = await _send_and_collect(client, msg)
        # Should produce a result
        assert text or files
        await client.close()

    @pytest.mark.asyncio
    async def test_inspect_form_with_file(self, tmp_path):
        docx_path = _make_sample_docx(tmp_path / "form.docx")
        docx_bytes = docx_path.read_bytes()

        client = await _connect(self.base_url)
        msg = _tool_call_message(
            "inspect_form",
            {"file_path": "form.docx"},
            files=[
                (
                    "form.docx",
                    docx_bytes,
                    "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                )
            ],
        )
        text, files = await _send_and_collect(client, msg)
        assert "FullName" in text
        await client.close()


# NL routing with mocked LLM


class TestA2ANaturalLanguage:
    """Test NL -> tool routing with mocked LLM decisions."""

    @pytest.fixture(autouse=True)
    def _agent(self, monkeypatch):
        self.port = free_port()
        self.base_url = f"http://127.0.0.1:{self.port}"

        # Mock the LLM executor to return predefined tool results
        self._mock_responses = []

        import semos.agentura.core.a2a_server as a2a_mod
        from semos.agentura.core.llm_executor import ExecutorResult

        class _MockExecutor(a2a_mod._AgentExecutor):
            _mock_ref = self._mock_responses

            async def _run_llm_executor(self_inner, text, files, task_id, **kw):
                if _MockExecutor._mock_ref:
                    tool_name, args = _MockExecutor._mock_ref.pop(0)
                    t, f = await self_inner._call_tool(
                        tool_name,
                        args,
                        files,
                    )
                    return ExecutorResult(text=t, files=f)
                return None

        monkeypatch.setattr(a2a_mod, "_AgentExecutor", _MockExecutor)

        from semos.agentura.document.service import _service

        monkeypatch.setattr(
            type(_service),
            "router_llm_model",
            property(lambda s: "mock-model"),
        )

        self.server, self.thread = start_agent(
            "document_agent",
            self.port,
        )
        yield
        self.server.should_exit = True
        self.thread.join(timeout=5)

    @needs_pandoc
    @pytest.mark.asyncio
    async def test_nl_compose_via_mocked_llm(self):
        """NL message routed to compose_document by mock LLM."""
        self._mock_responses.append(
            (
                "compose_document",
                {"source_markdown": "# NL Test\n\nGenerated.", "format": "html"},
            )
        )
        client = await _connect(self.base_url)
        # Send natural language (no DataPart, just text)
        msg = Message(
            message_id=str(uuid4()),
            role=Role.ROLE_USER,
            parts=[Part(text="Please compose an HTML document.")],
        )
        text, files = await _send_and_collect(client, msg)
        assert text or files
        await client.close()

    @pytest.mark.asyncio
    async def test_nl_inspect_with_file(self, tmp_path):
        """NL: 'inspect this form' with attached DOCX."""
        docx_path = _make_sample_docx(tmp_path / "form.docx")
        docx_bytes = docx_path.read_bytes()

        self._mock_responses.append(
            (
                "inspect_form",
                {"file_path": "form.docx"},
            )
        )
        client = await _connect(self.base_url)
        msg = Message(
            message_id=str(uuid4()),
            role=Role.ROLE_USER,
            parts=[
                Part(text="Please inspect this form."),
                Part(
                    raw=docx_bytes,
                    filename="form.docx",
                    media_type=("application/vnd.openxmlformats-officedocument.wordprocessingml.document"),
                ),
            ],
        )
        text, files = await _send_and_collect(client, msg)
        assert "FullName" in text
        await client.close()


# File round-trip via A2A (explicit tool calls)


class TestA2AFileRoundTrip:
    """Compose a file via A2A, then digest it back."""

    @pytest.fixture(autouse=True)
    def _agent(self):
        self.port = free_port()
        self.base_url = f"http://127.0.0.1:{self.port}"
        self.server, self.thread = start_agent(
            "document_agent",
            self.port,
        )
        yield
        self.server.should_exit = True
        self.thread.join(timeout=5)

    @needs_pandoc
    @pytest.mark.asyncio
    async def test_compose_then_digest(self):
        """Compose DOCX, fetch it, send back for digestion."""
        client = await _connect(self.base_url)

        # Step 1: Compose
        compose_msg = _tool_call_message(
            "compose_document",
            {"source_markdown": "# Round Trip\n\nContent.", "format": "docx"},
        )
        compose_text, compose_files = await _send_and_collect(
            client,
            compose_msg,
        )
        assert compose_text or compose_files

        # Extract download URL from A2A artifact
        assert compose_files, f"No files produced. text={compose_text!r}"
        download_url = compose_files[0].get("url", "")
        assert download_url, f"No URL in artifact: {compose_files}"

        # Step 2: Fetch file
        async with httpx.AsyncClient() as http:
            path = "/" + download_url.split("/", 3)[-1]
            resp = await http.get(
                f"{self.base_url}{path}",
                timeout=30,
            )
            resp.raise_for_status()
            file_bytes = resp.content

        # Step 3: Digest
        digest_msg = _tool_call_message(
            "digest_document",
            {"source": "composed.docx"},
            files=[
                (
                    "composed.docx",
                    file_bytes,
                    "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                )
            ],
        )
        digest_text, _ = await _send_and_collect(client, digest_msg)
        assert digest_text
        low = digest_text.lower()
        assert "round trip" in low or "content" in low
        await client.close()


# Email agent


class TestEmailA2A:
    @pytest.fixture(autouse=True)
    def _agent(self):
        self.port = free_port()
        self.base_url = f"http://127.0.0.1:{self.port}"
        self.server, self.thread = start_agent(
            "email_agent",
            self.port,
        )
        yield
        self.server.should_exit = True
        self.thread.join(timeout=5)

    @pytest.mark.asyncio
    async def test_search_via_a2a(self):
        client = await _connect(self.base_url)
        msg = _tool_call_message(
            "search_emails",
            {"query": "test", "limit": 3},
        )
        text, files = await _send_and_collect(client, msg)
        assert text
        await client.close()

    @pytest.mark.asyncio
    async def test_agent_card(self):
        url = f"{self.base_url}/.well-known/agent-card.json"
        async with httpx.AsyncClient() as http:
            resp = await http.get(url, timeout=5)
        assert resp.status_code == 200
        data = resp.json()
        assert data["name"] == "Email Agent"


# JSON-RPC binding test


class TestA2AJsonRpc:
    """Verify JSON-RPC binding works alongside REST."""

    @pytest.fixture(autouse=True)
    def _agent(self):
        self.port = free_port()
        self.base_url = f"http://127.0.0.1:{self.port}"
        self.server, self.thread = start_agent(
            "document_agent",
            self.port,
        )
        yield
        self.server.should_exit = True
        self.thread.join(timeout=5)

    @needs_pandoc
    @pytest.mark.asyncio
    async def test_compose_via_jsonrpc(self):
        """Call compose_document via JSON-RPC binding."""
        # ClientFactory defaults to JSONRPC when no config given
        client = await ClientFactory.connect(self.base_url)
        msg = _tool_call_message(
            "compose_document",
            {"source_markdown": "# RPC Test\n\nHello.", "format": "html"},
        )
        text, files = await _send_and_collect(client, msg)
        assert text or files
        await client.close()

    @pytest.mark.asyncio
    async def test_agent_card_has_both_bindings(self):
        url = f"{self.base_url}/.well-known/agent-card.json"
        async with httpx.AsyncClient() as http:
            resp = await http.get(url, timeout=5)
        data = resp.json()
        bindings = {i["protocolBinding"] for i in data.get("supportedInterfaces", [])}
        assert "HTTP+JSON" in bindings
        assert "JSONRPC" in bindings


# AgenturaClient.ask_agent tests


class TestAskAgent:
    """Test AgenturaClient.ask_agent() A2A delegation."""

    @pytest.fixture(autouse=True)
    def _agent(self):
        self.port = free_port()
        self.base_url = f"http://127.0.0.1:{self.port}"
        self.server, self.thread = start_agent(
            "document_agent",
            self.port,
        )
        yield
        self.server.should_exit = True
        self.thread.join(timeout=5)

    @pytest.mark.asyncio
    async def test_ask_agent_explicit_tool(self, tmp_path):
        """ask_agent dispatches to agent, gets result."""
        from semos.agentura.core.client import AgenturaClient

        async with AgenturaClient(
            {"document": self.base_url},
            download_dir=tmp_path,
        ) as client:
            result = await client.ask_agent(
                "document",
                "compose an HTML page with title Test",
            )
            assert not result.is_error, result.text
            assert result.status == "completed"
            assert result.text

    @pytest.mark.asyncio
    async def test_ask_agent_with_file(self, tmp_path):
        """ask_agent with file from registry."""
        from semos.agentura.core.client import AgenturaClient

        docx_path = _make_sample_docx(tmp_path / "form.docx")
        async with AgenturaClient(
            {"document": self.base_url},
            download_dir=tmp_path,
        ) as client:
            # Upload file to registry
            client.upload(docx_path)
            # Delegate with explicit file reference
            result = await client.ask_agent(
                "document",
                "inspect this form",
                files=["form.docx"],
            )
            # Should get some result (not necessarily FullName
            # since NL routing may not be configured)
            assert result.text
            assert result.status in (
                "completed",
                "input_required",
            )

    @pytest.mark.asyncio
    async def test_ask_agent_unknown(self, tmp_path):
        """ask_agent for unknown agent returns error."""
        from semos.agentura.core.client import AgenturaClient

        async with AgenturaClient(
            {"document": self.base_url},
            download_dir=tmp_path,
        ) as client:
            result = await client.ask_agent(
                "nonexistent",
                "hello",
            )
            assert result.is_error
            assert "not found" in result.text.lower()


# Multi-file A2A round-trip


class TestA2AMultiFileRoundTrip:
    """Verify multiple files from a single tool call survive
    the full SDK round-trip: server -> HTTP -> client.

    Regression test for: only first file returned via A2A.
    """

    @pytest.fixture(autouse=True)
    def _agent(self, monkeypatch, tmp_path):
        self.port = free_port()
        self.base_url = f"http://127.0.0.1:{self.port}"

        import semos.agentura.core.a2a_server as a2a_mod
        from semos.agentura.core.llm_executor import ExecutorResult

        # Create real files on disk so the URLs resolve
        pdf = tmp_path / "invoice.pdf"
        pdf.write_bytes(b"PDF-CONTENT")
        xml = tmp_path / "e-invoice.xml"
        xml.write_bytes(b"<xml>INVOICE</xml>")
        self._tmp_path = tmp_path

        class _MultiFileExecutor(a2a_mod._AgentExecutor):
            _pdf = pdf
            _xml = xml

            async def _run_llm_executor(
                self_inner,
                text,
                files,
                task_id,
                **kw,
            ):
                return ExecutorResult(
                    text="Email with 2 attachments.",
                    files=[
                        {
                            "filename": "invoice.pdf",
                            "download_url": f"files/{pdf.name}",
                            "mime_type": "application/pdf",
                            "size_bytes": pdf.stat().st_size,
                        },
                        {
                            "filename": "e-invoice.xml",
                            "download_url": f"files/{xml.name}",
                            "mime_type": "application/xml",
                            "size_bytes": xml.stat().st_size,
                        },
                    ],
                    status="completed",
                )

        monkeypatch.setattr(a2a_mod, "_AgentExecutor", _MultiFileExecutor)

        # Enable NL routing
        from semos.agentura.document.service import _service

        monkeypatch.setattr(
            type(_service),
            "router_llm_model",
            property(lambda s: "mock-model"),
        )
        # Point output_dir to tmp_path so /files/ serves from there
        _service.output_dir = tmp_path

        self.server, self.thread = start_agent(
            "document_agent",
            self.port,
        )
        yield
        self.server.should_exit = True
        self.thread.join(timeout=5)

    @pytest.mark.asyncio
    async def test_multiple_files_via_sdk(self):
        """Both files appear as artifacts after full HTTP round-trip."""
        client = await _connect(self.base_url)
        msg = Message(
            message_id=str(uuid4()),
            role=Role.ROLE_USER,
            parts=[Part(text="read the invoice email with attachments")],
        )
        text, files = await _send_and_collect(client, msg)
        assert len(files) == 2, f"Expected 2 files, got {len(files)}: {files}"
        names = {f["filename"] for f in files}
        assert "invoice.pdf" in names
        assert "e-invoice.xml" in names
        # All URLs absolute
        for f in files:
            assert f["url"].startswith("http://"), f"Relative URL: {f['url']}"
        await client.close()
