"""Integration tests for A2A protocol against auto-started agents.

Tests explicit tool calls via A2A JSON-RPC, agent card discovery,
and file round-trips. Uses a2a-sdk 1.0 Client API.
"""

from __future__ import annotations

from pathlib import Path
from uuid import uuid4
from zipfile import ZipFile

import httpx
import pytest
from google.protobuf.struct_pb2 import Struct, Value

from a2a.client import Client, ClientConfig, ClientFactory
from a2a.types import (
    Message,
    Part,
    Role,
    SendMessageRequest,
)

from conftest import free_port, start_agent


async def _connect(base_url: str) -> Client:
    """Connect to an A2A agent using REST (HTTP+JSON) binding."""
    config = ClientConfig(
        supported_protocol_bindings=["HTTP+JSON"],
        httpx_client=httpx.AsyncClient(timeout=60),
    )
    return await ClientFactory.connect(
        base_url, client_config=config,
    )


def _make_sample_docx(path: Path) -> Path:
    doc_xml = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<w:document xmlns:w="http://schemas.openxmlformats.org'
        '/wordprocessingml/2006/main"><w:body><w:p><w:sdt>'
        '<w:sdtPr><w:alias w:val="FullName"/></w:sdtPr>'
        '<w:sdtContent><w:p><w:r><w:t></w:t></w:r></w:p>'
        '</w:sdtContent></w:sdt></w:p></w:body></w:document>'
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
    tool: str, arguments: dict,
    files: list[tuple[str, bytes, str]] | None = None,
) -> Message:
    """Build an A2A Message for an explicit tool call."""
    data_struct = Struct()
    data_struct.update({"tool": tool, "arguments": arguments})
    parts = [Part(data=Value(struct_value=data_struct))]
    for fname, blob, mime in (files or []):
        parts.append(Part(
            raw=blob, filename=fname, media_type=mime,
        ))
    return Message(
        message_id=str(uuid4()),
        role=Role.ROLE_USER,
        parts=parts,
    )


async def _send_and_collect(
    client: Client, message: Message,
) -> tuple[str, list[dict]]:
    """Send message via A2A, collect final text and file artifacts."""
    request = SendMessageRequest(message=message)
    text_parts = []
    file_artifacts = []
    final_task = None

    async for (stream_resp, task) in client.send_message(request):
        if task:
            final_task = task

    if final_task:
        # Extract text from status message (Message with parts)
        if final_task.status and final_task.status.message:
            msg = final_task.status.message
            for p in msg.parts:
                if p.HasField("text"):
                    text_parts.append(p.text)
        # Extract file artifacts
        for art in final_task.artifacts:
            for p in art.parts:
                if p.HasField("url"):
                    file_artifacts.append({
                        "url": p.url,
                        "filename": p.filename,
                        "mime_type": p.media_type,
                    })

    return "\n".join(text_parts), file_artifacts


# Agent Card

class TestAgentCard:
    @pytest.fixture(autouse=True)
    def _agent(self):
        self.port = free_port()
        self.base_url = f"http://127.0.0.1:{self.port}"
        self.server, self.thread = start_agent(
            "document_agent", self.port,
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
            "document_agent", self.port,
        )
        yield
        self.server.should_exit = True
        self.thread.join(timeout=5)

    @pytest.mark.asyncio
    async def test_compose_html(self):
        client = await _connect(self.base_url)
        msg = _tool_call_message(
            "compose_document",
            {"source": "# A2A Test\n\nHello.", "format": "html"},
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
            files=[(
                "form.docx", docx_bytes,
                "application/vnd.openxmlformats-officedocument"
                ".wordprocessingml.document",
            )],
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

        # Mock the LLM router to return predefined tool selections
        self._mock_responses = []

        async def _mock_route(msg, tools, **kwargs):
            if self._mock_responses:
                return self._mock_responses.pop(0)
            return None

        monkeypatch.setattr(
            "agentura_commons.a2a_server._AgentExecutor"
            "._route_via_llm",
            None,
        )

        # Patch at module level so the executor picks it up
        import agentura_commons.a2a_server as a2a_mod
        self._orig_executor = a2a_mod._AgentExecutor

        class _MockExecutor(a2a_mod._AgentExecutor):
            _mock_responses_ref = self._mock_responses

            async def _route_via_llm(self_inner, text, files):
                if _MockExecutor._mock_responses_ref:
                    tool_name, args = (
                        _MockExecutor._mock_responses_ref.pop(0)
                    )
                    return await self_inner._call_tool(
                        tool_name, args, files,
                    )
                return None

        monkeypatch.setattr(
            a2a_mod, "_AgentExecutor", _MockExecutor,
        )

        # Enable router on the service so NL path is attempted
        from document_agent.service import _service
        monkeypatch.setattr(
            type(_service), "router_llm_model",
            property(lambda s: "mock-model"),
        )

        self.server, self.thread = start_agent(
            "document_agent", self.port,
        )
        yield
        self.server.should_exit = True
        self.thread.join(timeout=5)

    @pytest.mark.asyncio
    async def test_nl_compose_via_mocked_llm(self):
        """NL message routed to compose_document by mock LLM."""
        self._mock_responses.append((
            "compose_document",
            {"source": "# NL Test\n\nGenerated.", "format": "html"},
        ))
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

        self._mock_responses.append((
            "inspect_form",
            {"file_path": "form.docx"},
        ))
        client = await _connect(self.base_url)
        msg = Message(
            message_id=str(uuid4()),
            role=Role.ROLE_USER,
            parts=[
                Part(text="Please inspect this form."),
                Part(
                    raw=docx_bytes,
                    filename="form.docx",
                    media_type=(
                        "application/vnd.openxmlformats"
                        "-officedocument.wordprocessingml.document"
                    ),
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
            "document_agent", self.port,
        )
        yield
        self.server.should_exit = True
        self.thread.join(timeout=5)

    @pytest.mark.asyncio
    async def test_compose_then_digest(self):
        """Compose DOCX, fetch it, send back for digestion."""
        client = await _connect(self.base_url)

        # Step 1: Compose
        compose_msg = _tool_call_message(
            "compose_document",
            {"source": "# Round Trip\n\nContent.", "format": "docx"},
        )
        compose_text, compose_files = await _send_and_collect(
            client, compose_msg,
        )
        assert compose_text or compose_files

        # Extract download URL
        import json as _json
        download_url = ""
        try:
            data = _json.loads(compose_text)
            download_url = data.get("download_url", "")
        except (ValueError, TypeError):
            pass
        if compose_files:
            download_url = compose_files[0].get("url", download_url)
        assert download_url

        # Step 2: Fetch file
        async with httpx.AsyncClient() as http:
            path = "/" + download_url.split("/", 3)[-1]
            resp = await http.get(
                f"{self.base_url}{path}", timeout=30,
            )
            resp.raise_for_status()
            file_bytes = resp.content

        # Step 3: Digest
        digest_msg = _tool_call_message(
            "digest_document",
            {"source": "composed.docx"},
            files=[(
                "composed.docx", file_bytes,
                "application/vnd.openxmlformats-officedocument"
                ".wordprocessingml.document",
            )],
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
            "email_agent", self.port,
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
            "document_agent", self.port,
        )
        yield
        self.server.should_exit = True
        self.thread.join(timeout=5)

    @pytest.mark.asyncio
    async def test_compose_via_jsonrpc(self):
        """Call compose_document via JSON-RPC binding."""
        # ClientFactory defaults to JSONRPC when no config given
        client = await ClientFactory.connect(self.base_url)
        msg = _tool_call_message(
            "compose_document",
            {"source": "# RPC Test\n\nHello.", "format": "html"},
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
        bindings = {
            i["protocolBinding"] for i in
            data.get("supportedInterfaces", [])
        }
        assert "HTTP+JSON" in bindings
        assert "JSONRPC" in bindings
