"""Tests for backend abstraction - models, config, protocols, factory.

These tests use mocks and do NOT require Outlook, IMAP, or any credentials.
"""

from __future__ import annotations

import sys
from datetime import datetime
from unittest.mock import MagicMock

import pytest
from email_agent.backend import (
    IMAPBackend,
    _com_dict_to_email,
    _com_dict_to_event,
    _parse_com_datetime,
    create_backend,
)
from email_agent.config import Settings
from email_agent.exceptions import BackendNotAvailable
from email_agent.formatting import html_to_annotated_text, md_to_html, md_to_plain
from email_agent.models import EmailMessage, EventInfo
from email_agent.tools import (
    Recipient,
    _recipients_to_str,
    get_email_tools,
)

# Model tests


class TestEventInfo:
    def test_defaults(self):
        ev = EventInfo()
        assert ev.entry_id == ""
        assert ev.subject == ""
        assert ev.start is None

    def test_str(self):
        ev = EventInfo(
            subject="Team Meeting",
            start=datetime(2026, 3, 21, 10, 0),
            end=datetime(2026, 3, 21, 11, 0),
            location="Room 101",
        )
        s = str(ev)
        assert "Team Meeting" in s
        assert "Room 101" in s
        assert "2026-03-21 10:00" in s


class TestEmailMessage:
    def test_body_prefers_text(self):
        msg = EmailMessage(uid="1", body_text="plain", body_html="<p>html</p>")
        assert msg.body == "plain"

    def test_body_falls_back_to_html(self):
        msg = EmailMessage(uid="1", body_html="<p>html</p>")
        assert msg.body == "<p>html</p>"


# Config tests


class TestSettings:
    def test_detected_backend_imap(self):
        s = Settings(
            imap_host="imap.example.com",
            azure_client_id="test-id",
            email_address="user@example.com",
        )
        assert s.detected_backend == "imap"

    def test_detected_backend_explicit(self):
        s = Settings(backend="imap")
        assert s.detected_backend == "imap"

    def test_detected_backend_com_on_windows(self):
        s = Settings()
        if sys.platform == "win32":
            assert s.detected_backend == "com"

    def test_detected_backend_raises_on_linux_no_config(self):
        if sys.platform == "win32":
            pytest.skip("Only applies on non-Windows")
        s = Settings()
        with pytest.raises(BackendNotAvailable):
            _ = s.detected_backend

    def test_effective_mailgent_model_defaults(self):
        s = Settings()
        assert "claude" in s.effective_mailgent_model.lower() or "sonnet" in s.effective_mailgent_model.lower()

    def test_effective_mailgent_model_override(self):
        s = Settings(mailgent_model="gpt-4")
        assert s.effective_mailgent_model == "gpt-4"

    def test_extra_fields_ignored(self):
        # Settings should not fail with unknown env vars
        s = Settings(UNKNOWN_FIELD="whatever")
        assert s is not None


# COM dict conversion tests


class TestCOMConversion:
    def test_parse_com_datetime(self):
        assert _parse_com_datetime("2026-03-21 10:00:00+00:00") == datetime(2026, 3, 21, 10, 0)

    def test_parse_com_datetime_empty(self):
        assert _parse_com_datetime("") is None
        assert _parse_com_datetime(None) is None

    def test_com_dict_to_email(self):
        d = {
            "entry_id": "ABC123",
            "subject": "Test Email",
            "sender": "Alice",
            "sender_email": "alice@example.com",
            "to": "bob@example.com; carol@example.com",
            "cc": "",
            "received": "2026-03-21 10:00:00+00:00",
            "body": "Hello World",
            "has_attachments": True,
            "attachment_count": 1,
            "attachments": [{"filename": "doc.pdf", "size": 1024, "saved_path": None}],
        }
        msg = _com_dict_to_email(d)
        assert isinstance(msg, EmailMessage)
        assert msg.uid == "ABC123"
        assert msg.subject == "Test Email"
        assert msg.sender == "alice@example.com"
        assert msg.sender_name == "Alice"
        assert msg.to == ["bob@example.com", "carol@example.com"]
        assert msg.body_text == "Hello World"
        assert len(msg.attachments) == 1
        assert msg.attachments[0].filename == "doc.pdf"

    def test_com_dict_to_event(self):
        d = {
            "entry_id": "EVT1",
            "subject": "Meeting",
            "start": "2026-03-21 14:00:00+00:00",
            "end": "2026-03-21 15:00:00+00:00",
            "location": "Online",
            "all_day": False,
            "organizer": "Boss",
            "required": "alice@example.com",
        }
        ev = _com_dict_to_event(d)
        assert isinstance(ev, EventInfo)
        assert ev.subject == "Meeting"
        assert ev.start == datetime(2026, 3, 21, 14, 0)
        assert ev.location == "Online"
        assert ev.required_attendees == "alice@example.com"


# Formatting tests


class TestFormatting:
    def test_md_to_plain_headers(self):
        assert md_to_plain("## Heading") == "HEADING"

    def test_md_to_plain_bold(self):
        assert md_to_plain("**important**") == "IMPORTANT"

    def test_md_to_plain_unicode(self):
        result = md_to_plain("✓ done ✗ failed")
        assert "x" in result
        assert "-" in result

    def test_md_to_html_headings_become_bold(self):
        html = md_to_html("## Title")
        assert "<b>" in html
        assert "<h2>" not in html

    def test_md_to_html_style_applied(self):
        html = md_to_html("Hello world", style="font-size:11pt")
        assert "font-size:11pt" in html

    def test_html_to_annotated_text_strikethrough(self):
        result = html_to_annotated_text("<s>cancelled</s>")
        assert "~~cancelled~~" in result

    def test_html_to_annotated_text_highlight(self):
        result = html_to_annotated_text("<mark>important</mark>")
        assert "[HIGHLIGHT: important]" in result


# Recipient validation tests


class TestRecipientValidation:
    def test_valid_email(self):
        r = Recipient(email="alice@example.com")
        assert str(r) == "alice@example.com"

    def test_with_display_name(self):
        r = Recipient(name="Alice", email="alice@example.com")
        assert str(r) == "Alice <alice@example.com>"

    def test_invalid_email_rejected(self):
        with pytest.raises(ValueError):
            Recipient(email="not-an-email")

    def test_empty_name_omitted(self):
        r = Recipient(name="", email="a@b.com")
        assert str(r) == "a@b.com"

    def test_multiple_recipients_to_str(self):
        recipients = [
            Recipient(name="Alice", email="alice@example.com"),
            Recipient(email="bob@test.org"),
        ]
        result = _recipients_to_str(recipients)
        assert result == "Alice <alice@example.com>; bob@test.org"

    def test_display_name_preserved(self):
        r = Recipient(name="Alice Smith", email="alice@example.com")
        assert "Alice Smith" in str(r)
        assert "alice@example.com" in str(r)


# Tool schema tests


class TestToolSchemas:
    def test_expected_tool_names(self):
        stub = MagicMock()
        stub.run_sync = MagicMock()
        stub.output_dir = None
        tools = get_email_tools(stub)
        names = {t.name for t in tools}
        expected = {
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
        assert names == expected

    def test_all_tools_have_schemas(self):
        stub = MagicMock()
        tools = get_email_tools(stub)
        for t in tools:
            schema = t.get_input_schema()
            assert schema is not None


# Tool execution with mock backend


def _mock_service():
    """Create a mock service with run_sync that calls fn(backend) directly."""
    backend = MagicMock()
    backend.supports_com = False
    backend.calendar = None

    service = MagicMock()

    async def mock_run_sync(fn):
        return fn(backend)

    service.run_sync = mock_run_sync
    service.output_dir = None
    service.resolve_file = MagicMock()
    return service, backend


class TestToolExecution:
    def test_search_emails(self):
        import asyncio

        service, backend = _mock_service()
        msg = EmailMessage(
            uid="1",
            subject="Hello",
            sender="alice@example.com",
            sender_name="Alice",
            date=datetime(2026, 3, 21),
        )
        backend.search_emails.return_value = [msg]

        tools = get_email_tools(service)
        search = next(t for t in tools if t.name == "search_emails")
        result = asyncio.run(search._arun(query="Hello"))
        assert "Hello" in result
        assert "alice@example.com" in result

    def test_list_events_no_calendar(self):
        import asyncio

        service, backend = _mock_service()
        tools = get_email_tools(service)
        le = next(t for t in tools if t.name == "list_events")
        result = asyncio.run(le._arun(days=7))
        assert "not available" in result

    def test_create_draft_validates_email(self):
        with pytest.raises(ValueError):
            Recipient(email="not-an-email")

    def test_create_draft_success(self):
        import asyncio

        service, backend = _mock_service()
        backend.create_draft.return_value = "ENTRY123"
        tools = get_email_tools(service)
        draft = next(t for t in tools if t.name == "create_draft")
        result = asyncio.run(
            draft._arun(
                to=[{"email": "alice@example.com"}],
                subject="Test",
                body="Hello **world**",
            )
        )
        assert "draft created" in result
        # Verify markdown was converted to plain text
        call_args = backend.create_draft.call_args
        body = call_args.kwargs.get("body", "")
        assert "**" not in body

    def test_draft_reply(self):
        import asyncio

        service, backend = _mock_service()
        msg = EmailMessage(
            uid="1",
            subject="Re: Hello",
            sender="bob@test.org",
            sender_name="Bob",
        )
        backend.search_emails.return_value = [msg]
        backend.draft_reply.return_value = "reply-123"

        tools = get_email_tools(service)
        reply = next(t for t in tools if t.name == "draft_reply")
        result = asyncio.run(reply._arun(uid="msg-123", body="Thanks!"))
        assert "reply draft created" in result


# Factory tests


class TestFactory:
    def test_create_imap_backend(self):
        s = Settings(
            backend="imap",
            imap_host="imap.example.com",
            azure_client_id="test-id",
            email_address="user@example.com",
        )
        b = create_backend(s)
        assert isinstance(b, IMAPBackend)
        assert not b.supports_com
        assert b.calendar is None

    @pytest.mark.skipif(sys.platform != "win32", reason="COM only on Windows")
    def test_create_com_backend(self):
        import pythoncom

        pythoncom.CoInitialize()
        try:
            s = Settings(backend="com")
            b = create_backend(s)
            from email_agent.backend import COMBackend

            assert isinstance(b, COMBackend)
            assert b.supports_com
            assert b.calendar is not None
        finally:
            pythoncom.CoUninitialize()

    def test_graph_not_implemented(self):
        s = Settings(backend="graph")
        with pytest.raises(BackendNotAvailable, match="not yet implemented"):
            create_backend(s)

    def test_unknown_backend_raises(self):
        s = Settings(backend="unknown")
        with pytest.raises(BackendNotAvailable):
            create_backend(s)


# IMAPBackend unit tests (mocked MailClient)


class TestIMAPBackend:
    def test_search_emails_delegates(self):
        s = Settings(
            backend="imap",
            imap_host="imap.example.com",
            azure_client_id="test-id",
            email_address="user@example.com",
        )
        b = IMAPBackend(s)
        mock_client = MagicMock()
        mock_client.search.return_value = ["1", "2"]
        mock_client.get_message.side_effect = [
            EmailMessage(uid="1", subject="A"),
            EmailMessage(uid="2", subject="B"),
        ]
        b._client = mock_client
        results = b.search_emails("test")
        assert len(results) == 2
        assert results[0].subject == "A"

    def test_calendar_is_none_by_default(self):
        s = Settings(backend="imap", imap_host="x", azure_client_id="x", email_address="x@x.com")
        b = IMAPBackend(s)
        assert b.calendar is None
        assert not b.supports_com

    def test_calendar_can_be_set(self):
        s = Settings(backend="imap", imap_host="x", azure_client_id="x", email_address="x@x.com")
        b = IMAPBackend(s)
        mock_cal = MagicMock()
        b.calendar = mock_cal
        assert b.calendar is mock_cal
