"""Tests for __main__.py - app setup, callback wrapping, file upload."""

from __future__ import annotations

import base64
from unittest.mock import AsyncMock, MagicMock

import pytest
from mcp.types import CallToolResult, TextContent

from agentura_ui.file_registry import FileRegistry
from agentura_ui.mcp_hub import AgentConnection
from agentura_ui.mcp_tools import _make_mcp_tool_class


# _build_agents


class TestBuildAgents:
    def test_default_urls(self, monkeypatch):
        monkeypatch.delenv("EMAIL_AGENT_URL", raising=False)
        monkeypatch.delenv("DOCUMENT_AGENT_URL", raising=False)
        from agentura_ui.__main__ import _build_agents

        agents = _build_agents()
        assert len(agents) == 2
        assert agents[0].name == "email-agent"
        assert "8001" in agents[0].url
        assert agents[1].name == "document-agent"
        assert "8002" in agents[1].url

    def test_from_env(self, monkeypatch):
        monkeypatch.setenv(
            "EMAIL_AGENT_URL", "http://custom:9001/mcp/sse",
        )
        monkeypatch.setenv(
            "DOCUMENT_AGENT_URL", "http://custom:9002/mcp/sse",
        )
        from agentura_ui.__main__ import _build_agents

        agents = _build_agents()
        assert "9001" in agents[0].url
        assert "9002" in agents[1].url


# _register_litellm_provider


class TestRegisterLitellmProvider:
    def test_idempotent(self):
        from panelini.panels.ai.utils.ai_interface import (
            PROVIDER_CLASS_REGISTRY,
        )
        from agentura_ui.__main__ import _register_litellm_provider

        _register_litellm_provider()
        first = PROVIDER_CLASS_REGISTRY.get("litellm")
        _register_litellm_provider()
        second = PROVIDER_CLASS_REGISTRY.get("litellm")
        assert first is second

    def test_registered(self):
        from panelini.panels.ai.utils.ai_interface import (
            PROVIDER_CLASS_REGISTRY,
        )
        from agentura_ui.__main__ import _register_litellm_provider

        _register_litellm_provider()
        assert "litellm" in PROVIDER_CLASS_REGISTRY


# _wrap_chat_callback


class TestWrapChatCallback:
    @pytest.fixture
    def setup(self):
        from agentura_ui.__main__ import _wrap_chat_callback
        from agentura_ui.file_manager import FileManager

        registry = FileRegistry()
        pending_uploads: list[str] = []
        file_mgr = FileManager(registry, pending_uploads)

        async def original(contents, user, instance):
            yield f"Echo: {contents}"

        wrapped = _wrap_chat_callback(
            original, registry, pending_uploads, file_mgr,
        )
        instance = MagicMock()
        instance.send = MagicMock()

        return wrapped, registry, pending_uploads, instance

    async def test_text_message_delegates(self, setup):
        wrapped, _, _, instance = setup

        chunks = []
        async for chunk in wrapped(
            "Hello world", "User", instance,
        ):
            chunks.append(chunk)

        assert "Echo: Hello world" in "".join(chunks)

    async def test_pending_uploads_prepended(self, setup):
        """After file upload, next text message gets context."""
        wrapped, _, pending_uploads, instance = setup

        # Simulate: file was registered in sidebar
        pending_uploads.append("doc.pdf (1.0 KB)")

        chunks = []
        async for chunk in wrapped(
            "describe it", "User", instance,
        ):
            chunks.append(chunk)

        text = "".join(chunks)
        assert "Uploaded files available" in text
        assert "doc.pdf" in text
        # pending_uploads should be cleared
        assert len(pending_uploads) == 0

    async def test_empty_message_guard(self, setup):
        wrapped, _, _, instance = setup

        chunks = []
        async for chunk in wrapped("  ", "User", instance):
            chunks.append(chunk)

        assert "Please enter a message" in "".join(chunks)

    async def test_non_string_guard(self, setup):
        """Non-string content (e.g. from ChatInterface FileInput
        tab if still present) is blocked."""
        wrapped, _, _, instance = setup

        chunks = []
        async for chunk in wrapped(
            b"some-bytes", "User", instance,
        ):
            chunks.append(chunk)

        assert "Please enter a message" in "".join(chunks)


# Full scenario: upload => "describe the file" => digest tool


class TestFileUploadThenDigest:
    """End-to-end: register a PDF in the registry (as the
    sidebar FileInput watcher would), send 'describe the file',
    verify digest_document is called with base64 content."""

    async def test_upload_then_digest_tool_call(
        self, digest_tool,
    ):
        from agentura_ui.__main__ import _wrap_chat_callback

        # Shared registry between sidebar upload and tool
        registry = FileRegistry()
        pending_uploads: list[str] = []

        # 1. Mock hub that records what digest_document receives
        hub = MagicMock()
        hub.agent_for_tool.return_value = AgentConnection(
            "doc", "http://x/mcp/sse", "http://x",
        )
        hub.call_tool = AsyncMock(
            return_value=CallToolResult(
                content=[
                    TextContent(
                        type="text",
                        text='{"markdown": "# Checklist"}',
                    ),
                ],
            ),
        )

        # 2. Create tool wrapper using the SAME registry
        tool = _make_mcp_tool_class(
            digest_tool, hub, registry,
        )

        # 3. Simulate what the sidebar FileInput watcher does:
        #    register file + append to pending_uploads
        pdf_bytes = b"%PDF-1.4 Checklist content here"
        registry.register(
            "Checklist.pdf", pdf_bytes,
            "application/pdf", "upload",
        )
        pending_uploads.append(
            "Checklist.pdf (35 B)",
        )

        # Verify file is in registry
        assert registry.get("Checklist.pdf") is not None

        # 4. Build the callback wrapper.
        #    Simulates panelini calling digest_document.
        async def fake_frontend_callback(
            contents, user, instance,
        ):
            result = await tool._arun(
                source="Checklist.pdf",
            )
            yield result

        from agentura_ui.file_manager import FileManager

        file_mgr = FileManager(registry, pending_uploads)
        wrapped = _wrap_chat_callback(
            fake_frontend_callback, registry,
            pending_uploads, file_mgr,
        )
        instance = MagicMock()
        instance.send = MagicMock()

        # 5. Send "describe the file"
        describe_chunks = []
        async for chunk in wrapped(
            "describe the file", "User", instance,
        ):
            describe_chunks.append(chunk)

        # 6. Verify: hub.call_tool received FileAttachment
        hub.call_tool.assert_called_once()
        call_args = hub.call_tool.call_args[0]
        tool_name = call_args[0]
        tool_args = call_args[1]

        assert tool_name == "digest_document"
        att = tool_args["source"]
        assert isinstance(att, dict)
        assert att["name"] == "Checklist.pdf"
        assert att["content"].startswith(
            "data:application/pdf;base64,"
        )

        # Base64 decodes back to original PDF bytes
        _, b64 = att["content"].split(",", 1)
        assert base64.b64decode(b64) == pdf_bytes

        # 7. Tool result reaches the user
        response = "".join(describe_chunks)
        assert "Checklist" in response

        # 8. Pending uploads were consumed
        assert len(pending_uploads) == 0
