"""MCP integration tests for email-agent.

Tests that all tools are reachable and functional via the MCP protocol,
using an in-memory transport (no uvicorn needed).

Uses a mocked backend to avoid requiring Outlook/IMAP.
"""

from __future__ import annotations

from datetime import datetime
from unittest.mock import MagicMock

import pytest
from agentura_commons.testing import mcp_client_for, parse_tool_result
from email_agent.models import EmailMessage
from email_agent.service import EmailAgentService


def _mock_backend():
    """Create a mock EmailBackend with realistic return values."""
    backend = MagicMock()
    backend.connect.return_value = None
    backend.supports_com = False
    backend.raw_com = None
    backend.calendar = None

    # search_emails returns EmailMessage objects
    backend.search_emails.return_value = [
        EmailMessage(
            uid="123",
            subject="Test email",
            sender="alice@example.com",
            sender_name="Alice",
            to=["bob@example.com"],
            cc=[],
            date=datetime(2026, 3, 20, 10, 0),
            body_text="Hello from test",
            body_html="",
            attachments=[],
        ),
    ]

    # get_message returns a full email
    backend.get_message.return_value = EmailMessage(
        uid="123",
        subject="Test email",
        sender="alice@example.com",
        sender_name="Alice",
        to=["bob@example.com"],
        cc=["carol@example.com"],
        date=datetime(2026, 3, 20, 10, 0),
        body_text="Full body of the test email",
        body_html="<p>Full body of the test email</p>",
        attachments=[],
    )

    backend.create_draft.return_value = "draft-456"
    backend.draft_reply.return_value = "reply-789"
    backend.send_reply.return_value = None
    backend.mark_as_read.return_value = None

    return backend


@pytest.fixture
def service():
    """Create an EmailAgentService with a mocked backend."""
    svc = EmailAgentService()
    mock_backend = _mock_backend()

    # Mock run_sync to call fn(backend) directly
    async def _mock_run_sync(fn):
        return fn(mock_backend)

    svc.run_sync = _mock_run_sync

    return svc


# Tool discovery


@pytest.mark.asyncio
async def test_list_tools(service):
    async with mcp_client_for(service) as client:
        result = await client.list_tools()
        names = {t.name for t in result.tools}
        assert names == {
            "search_emails",
            "read_email",
            "list_events",
            "free_slots",
            "create_draft",
            "draft_event",
            "send_event",
            "draft_reply",
            "send_reply",
        }


@pytest.mark.asyncio
async def test_tools_have_typed_parameters(service):
    """Every tool should have proper JSON Schema params (not **kwargs)."""
    async with mcp_client_for(service) as client:
        result = await client.list_tools()
        for tool in result.tools:
            schema = tool.inputSchema
            assert schema.get("type") == "object", f"{tool.name}: no object schema"
            props = schema.get("properties", {})
            assert "kwargs" not in props, f"{tool.name}: **kwargs leak in schema"


@pytest.mark.asyncio
async def test_search_has_query_param(service):
    """Regression: search_emails must expose 'query' parameter."""
    async with mcp_client_for(service) as client:
        result = await client.list_tools()
        search = next(t for t in result.tools if t.name == "search_emails")
        props = search.inputSchema["properties"]
        assert "query" in props
        assert props["query"]["type"] == "string"


# search_emails via MCP


@pytest.mark.asyncio
async def test_search_emails(service):
    async with mcp_client_for(service) as client:
        result = await client.call_tool("search_emails", {"query": "test"})
        data = parse_tool_result(result)
        assert isinstance(data, list)
        assert len(data) == 1
        assert data[0]["subject"] == "Test email"
        assert data[0]["sender_email"] == "alice@example.com"


@pytest.mark.asyncio
async def test_search_emails_with_limit(service):
    async with mcp_client_for(service) as client:
        result = await client.call_tool("search_emails", {"query": "test", "limit": 5})
        data = parse_tool_result(result)
        assert isinstance(data, list)


# read_email via MCP


@pytest.mark.asyncio
async def test_read_email(service):
    async with mcp_client_for(service) as client:
        result = await client.call_tool("read_email", {"query": "test"})
        data = parse_tool_result(result)
        assert data["subject"] == "Test email"
        assert "Full body" in data["body"]
        assert data["cc"] == "carol@example.com"


# create_draft via MCP


@pytest.mark.asyncio
async def test_create_draft(service):
    async with mcp_client_for(service) as client:
        result = await client.call_tool(
            "create_draft",
            {
                "to": [{"email": "bob@example.com"}],
                "subject": "Hello",
                "body": "Test body",
            },
        )
        assert not result.isError, f"Tool error: {result.content}"
        data = parse_tool_result(result)
        assert isinstance(data, dict), f"Expected dict, got: {data!r}"
        assert data["status"] == "draft created"
        assert data["entry_id"] == "draft-456"


@pytest.mark.asyncio
async def test_create_draft_validates_email(service):
    async with mcp_client_for(service) as client:
        result = await client.call_tool(
            "create_draft",
            {
                "to": [{"email": "not-an-email"}],
                "subject": "Hello",
                "body": "Test body",
            },
        )
        assert result.isError


# draft_reply / send_reply via MCP


@pytest.mark.asyncio
async def test_draft_reply(service):
    async with mcp_client_for(service) as client:
        result = await client.call_tool(
            "draft_reply",
            {"uid": "msg-123", "body": "Thanks!"},
        )
        data = parse_tool_result(result)
        assert data["status"] == "reply draft created"


@pytest.mark.asyncio
async def test_send_reply(service):
    async with mcp_client_for(service) as client:
        result = await client.call_tool(
            "send_reply",
            {"uid": "msg-123", "body": "Thanks!"},
        )
        data = parse_tool_result(result)
        assert data["status"] == "reply sent"


# calendar tools (no calendar backend)


@pytest.mark.asyncio
async def test_list_events_no_calendar(service):
    """Calendar tools should return an error when no calendar backend."""
    async with mcp_client_for(service) as client:
        result = await client.call_tool("list_events", {"days": 7})
        assert result.isError


@pytest.mark.asyncio
async def test_free_slots_no_calendar(service):
    async with mcp_client_for(service) as client:
        result = await client.call_tool("free_slots", {"days": 7})
        assert result.isError
