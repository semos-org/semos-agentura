"""Tests for backend abstraction - models, config, protocols, factory.

These tests use mocks and do NOT require Outlook, IMAP, or any credentials.
"""

from __future__ import annotations

import sys
from datetime import datetime, timedelta
from unittest.mock import MagicMock, patch

import pytest

from email_agent.config import Settings
from email_agent.exceptions import BackendNotAvailable, CalendarNotSupported, COMError
from email_agent.models import Attachment, EmailMessage, EventInfo
from email_agent.backend import (
    IMAPBackend, CalendarBackend, EmailBackend,
    _com_dict_to_email, _com_dict_to_event, _parse_com_datetime,
    create_backend,
)
from email_agent.formatting import md_to_plain, md_to_html, html_to_annotated_text
from email_agent.tools import ToolExecutor, _validate_email_list, TOOL_DEFINITIONS


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


# Email validation tests


class TestEmailValidation:
    def test_valid_emails(self):
        clean, errors = _validate_email_list("alice@example.com; bob@test.org")
        assert clean == "alice@example.com; bob@test.org"
        assert errors == []

    def test_display_name_stripped(self):
        clean, errors = _validate_email_list("Alice <alice@example.com>")
        assert clean == "alice@example.com"

    def test_invalid_email_reported(self):
        clean, errors = _validate_email_list("not-an-email")
        assert clean == ""
        assert len(errors) == 1

    def test_empty_input(self):
        clean, errors = _validate_email_list("")
        assert clean == ""
        assert errors == []

    def test_mixed_separators(self):
        clean, errors = _validate_email_list("a@b.com, c@d.com; e@f.com")
        assert clean == "a@b.com; c@d.com; e@f.com"


# Tool definitions test


class TestToolDefinitions:
    def test_all_tools_have_required_fields(self):
        for tool in TOOL_DEFINITIONS:
            assert tool["type"] == "function"
            assert "name" in tool["function"]
            assert "description" in tool["function"]
            assert "parameters" in tool["function"]

    def test_expected_tool_names(self):
        names = {t["function"]["name"] for t in TOOL_DEFINITIONS}
        expected = {
            "search_emails", "read_email", "list_events", "free_slots",
            "create_draft", "draft_event", "send_event",
            "draft_reply", "send_reply",
        }
        assert names == expected


# ToolExecutor with mock backend


class TestToolExecutor:
    def _mock_backend(self):
        backend = MagicMock()
        backend.supports_com = False
        backend.calendar = None
        return backend

    def test_search_emails(self):
        backend = self._mock_backend()
        msg = EmailMessage(
            uid="1", subject="Hello", sender="alice@example.com",
            sender_name="Alice", date=datetime(2026, 3, 21),
        )
        backend.search_emails.return_value = [msg]

        executor = ToolExecutor(backend)
        result = executor.execute("search_emails", {"query": "Hello"})
        assert "Hello" in result
        assert "alice@example.com" in result

    def test_list_events_no_calendar(self):
        backend = self._mock_backend()
        executor = ToolExecutor(backend)
        result = executor.execute("list_events", {"days": 7})
        assert "not available" in result

    def test_create_draft_validates_email(self):
        backend = self._mock_backend()
        executor = ToolExecutor(backend)
        result = executor.execute("create_draft", {
            "to": "not-an-email",
            "subject": "Test",
            "body": "Body",
        })
        assert "Invalid" in result

    def test_create_draft_success(self):
        backend = self._mock_backend()
        backend.create_draft.return_value = "ENTRY123"
        executor = ToolExecutor(backend)
        result = executor.execute("create_draft", {
            "to": "alice@example.com",
            "subject": "Test",
            "body": "Hello **world**",
        })
        assert "draft created" in result
        # Verify markdown was converted
        call_args = backend.create_draft.call_args
        assert "**" not in call_args.kwargs.get("body", call_args[1].get("body", ""))

    def test_unknown_tool(self):
        backend = self._mock_backend()
        executor = ToolExecutor(backend)
        result = executor.execute("nonexistent_tool", {})
        assert "Unknown tool" in result


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
        s = Settings(backend="com")
        b = create_backend(s)
        from email_agent.backend import COMBackend
        assert isinstance(b, COMBackend)
        assert b.supports_com
        assert b.calendar is not None

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
            backend="imap", imap_host="imap.example.com",
            azure_client_id="test-id", email_address="user@example.com",
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
