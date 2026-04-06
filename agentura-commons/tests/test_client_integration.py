"""Integration tests for AgenturaClient with auto-started agents.

Agents are started on random ports for the duration of each test class.
No manual agent startup needed.

Run with: pytest agentura-commons/tests/test_client_integration.py -v
"""

from __future__ import annotations

import json
from pathlib import Path
from zipfile import ZipFile

import pytest

from agentura_commons.client import AgenturaClient

from conftest import free_port, start_agent


def _make_sample_docx(path: Path) -> Path:
    """Create a minimal DOCX with a content control field."""
    doc_xml = """\
<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<w:document xmlns:w=\
"http://schemas.openxmlformats.org/wordprocessingml/2006/main">
<w:body>
  <w:p>
    <w:sdt>
      <w:sdtPr>
        <w:alias w:val="FullName"/>
      </w:sdtPr>
      <w:sdtContent>
        <w:p><w:r><w:t></w:t></w:r></w:p>
      </w:sdtContent>
    </w:sdt>
  </w:p>
</w:body>
</w:document>"""
    rels = """\
<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns=\
"http://schemas.openxmlformats.org/package/2006/relationships">
  <Relationship Id="rId1"
    Type="http://schemas.openxmlformats.org/\
officeDocument/2006/relationships/officeDocument"
    Target="word/document.xml"/>
</Relationships>"""
    ct = """\
<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Types xmlns=\
"http://schemas.openxmlformats.org/package/2006/content-types">
  <Default Extension="rels" ContentType=\
"application/vnd.openxmlformats-package.relationships+xml"/>
  <Default Extension="xml" ContentType="application/xml"/>
  <Override PartName="/word/document.xml"
    ContentType="application/vnd.openxmlformats-\
officedocument.wordprocessingml.document.main+xml"/>
</Types>"""
    with ZipFile(path, "w") as zf:
        zf.writestr("[Content_Types].xml", ct)
        zf.writestr("_rels/.rels", rels)
        zf.writestr("word/document.xml", doc_xml)
    return path


# Document agent tests

class TestDocumentAgent:
    """Tests against auto-started document-agent."""

    @pytest.fixture(autouse=True)
    def _agent(self):
        """Start document-agent on a random port."""
        self.port = free_port()
        self.base_url = f"http://127.0.0.1:{self.port}"
        self.server, self.thread = start_agent(
            "document_agent", self.port,
        )
        yield
        self.server.should_exit = True
        self.thread.join(timeout=5)

    @pytest.mark.asyncio
    async def test_connect_and_list_tools(self, tmp_path):
        async with AgenturaClient(
            {"document": self.base_url},
            download_dir=tmp_path,
        ) as client:
            names = {t.name for t in client.tools}
            assert "digest_document" in names
            assert "compose_document" in names
            assert "inspect_form" in names
            assert "fill_form" in names
            assert "generate_diagram" in names

    @pytest.mark.asyncio
    async def test_upload_and_inspect_form(self, tmp_path):
        docx_path = _make_sample_docx(tmp_path / "form.docx")
        async with AgenturaClient(
            {"document": self.base_url},
            download_dir=tmp_path,
        ) as client:
            name = client.upload(docx_path)
            assert name == "form.docx"

            result = await client.call_tool(
                "inspect_form", {"file_path": "form.docx"},
            )
            assert not result.is_error, result.text
            data = json.loads(result.text)
            if isinstance(data, dict) and "items" in data:
                data = data["items"]
            names = {f["name"] for f in data}
            assert "FullName" in names

    @pytest.mark.asyncio
    async def test_compose_downloads_file(self, tmp_path):
        async with AgenturaClient(
            {"document": self.base_url},
            download_dir=tmp_path,
        ) as client:
            result = await client.call_tool(
                "compose_document",
                {"source": "# Test\n\nHello.", "format": "html"},
            )
            assert not result.is_error, result.text
            assert len(result.files) == 1
            assert result.files[0].filename.endswith(".html")
            assert result.files[0].size > 0
            assert client.registry.get(
                result.files[0].filename,
            ) is not None
            assert "http://" not in result.text

    @pytest.mark.asyncio
    async def test_compose_docx_with_filename(self, tmp_path):
        async with AgenturaClient(
            {"document": self.base_url},
            download_dir=tmp_path,
        ) as client:
            result = await client.call_tool(
                "compose_document",
                {
                    "source": "# Report\n\nContent.",
                    "format": "docx",
                    "filename": "report.docx",
                },
            )
            assert not result.is_error, result.text
            assert len(result.files) == 1
            assert result.files[0].filename == "report.docx"

    @pytest.mark.asyncio
    async def test_compose_then_digest_roundtrip(self, tmp_path):
        async with AgenturaClient(
            {"document": self.base_url},
            download_dir=tmp_path,
        ) as client:
            compose = await client.call_tool(
                "compose_document",
                {
                    "source": "# Round Trip\n\nVerify content.",
                    "format": "docx",
                },
            )
            assert not compose.is_error
            produced = compose.files[0].filename

            digest = await client.call_tool(
                "digest_document",
                {"source": produced},
            )
            assert not digest.is_error, digest.text
            text = digest.text.lower()
            assert "mock" in text or "round trip" in text or "verify" in text

    @pytest.mark.asyncio
    async def test_unknown_tool(self, tmp_path):
        async with AgenturaClient(
            {"document": self.base_url},
            download_dir=tmp_path,
        ) as client:
            result = await client.call_tool("no_such_tool", {})
            assert result.is_error

    @pytest.mark.asyncio
    async def test_missing_required_param(self, tmp_path):
        async with AgenturaClient(
            {"document": self.base_url},
            download_dir=tmp_path,
        ) as client:
            result = await client.call_tool(
                "compose_document", {},
            )
            assert result.is_error


# Email agent tests

class TestEmailAgent:
    """Tests against auto-started email-agent."""

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
    async def test_connect_and_list_tools(self, tmp_path):
        async with AgenturaClient(
            {"email": self.base_url},
            download_dir=tmp_path,
        ) as client:
            names = {t.name for t in client.tools}
            assert "search_emails" in names
            assert "create_draft" in names
            assert "read_email" in names

    @pytest.mark.asyncio
    async def test_search_emails(self, tmp_path):
        async with AgenturaClient(
            {"email": self.base_url},
            download_dir=tmp_path,
        ) as client:
            result = await client.call_tool(
                "search_emails",
                {"query": "test", "limit": 3},
            )
            assert not result.is_error, result.text


# Multi-agent tests

class TestMultiAgent:
    """Tests with both agents running."""

    @pytest.fixture(autouse=True)
    def _agents(self):
        self.doc_port = free_port()
        self.email_port = free_port()
        self.doc_server, self.doc_thread = start_agent(
            "document_agent", self.doc_port,
        )
        self.email_server, self.email_thread = start_agent(
            "email_agent", self.email_port,
        )
        yield
        self.doc_server.should_exit = True
        self.email_server.should_exit = True
        self.doc_thread.join(timeout=5)
        self.email_thread.join(timeout=5)

    @pytest.mark.asyncio
    async def test_both_agents_tools(self, tmp_path):
        async with AgenturaClient(
            {
                "document": f"http://127.0.0.1:{self.doc_port}",
                "email": f"http://127.0.0.1:{self.email_port}",
            },
            download_dir=tmp_path,
        ) as client:
            names = {t.name for t in client.tools}
            assert "search_emails" in names
            assert "digest_document" in names
            assert "compose_document" in names
