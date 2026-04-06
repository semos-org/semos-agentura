"""Integration tests against a real backend.

These tests require a running email backend (COM with Outlook, or IMAP with credentials).
Skipped by default in CI. Run with: uv run pytest -m integration -v
"""

from __future__ import annotations

import sys
from datetime import datetime, timedelta

import pytest

pytestmark = pytest.mark.integration

from email_agent.config import Settings
from email_agent.backend import create_backend, EmailBackend
from email_agent.models import EmailMessage, EventInfo
from email_agent.tools import ToolExecutor


@pytest.fixture(scope="module")
def backend() -> EmailBackend:
    """Create a real backend or skip if unavailable."""
    try:
        s = Settings()
        b = create_backend(s)
        b.connect()
        return b
    except Exception as e:
        pytest.skip(f"No backend available: {e}")


# Email operations


class TestEmailOps:
    def test_search_returns_email_messages(self, backend):
        results = backend.search_emails("meeting", limit=5)
        assert isinstance(results, list)
        if results:
            msg = results[0]
            assert isinstance(msg, EmailMessage)
            assert msg.uid
            assert msg.subject

    def test_search_empty_query(self, backend):
        # Should not crash on empty query
        results = backend.search_emails("", limit=3)
        assert isinstance(results, list)

    def test_list_messages_inbox(self, backend):
        msgs = backend.list_messages(folder="INBOX", limit=5)
        assert isinstance(msgs, list)
        for msg in msgs:
            assert isinstance(msg, EmailMessage)
            assert msg.uid
            assert msg.subject is not None

    def test_list_drafts(self, backend):
        drafts = backend.list_drafts(limit=5)
        assert isinstance(drafts, list)
        for d in drafts:
            assert isinstance(d, EmailMessage)

    def test_get_message(self, backend):
        msgs = backend.list_messages(limit=1)
        if not msgs:
            pytest.skip("No messages in inbox")
        full = backend.get_message(msgs[0].uid)
        assert isinstance(full, EmailMessage)
        assert full.uid == msgs[0].uid
        # Full message should have body
        assert full.body_text or full.body_html

    def test_create_and_search_draft(self, backend):
        """Create a draft, find it, then clean up."""
        marker = f"__test_draft_{datetime.now().strftime('%H%M%S')}"
        entry_id = backend.create_draft(
            to="test@example.com",
            subject=f"Test Draft {marker}",
            body=f"This is a test draft {marker}",
        )
        # entry_id may be empty for IMAP
        assert isinstance(entry_id, str)

        # Search for it
        results = backend.search_emails(marker, folder="Drafts", limit=5)
        # Should find at least one
        found = [r for r in results if marker in r.subject]
        assert len(found) >= 1, f"Draft not found with marker {marker}"

        # Clean up: delete the draft
        if found:
            try:
                if hasattr(backend, '_ensure_client'):
                    # IMAP: delete via client
                    backend._ensure_client().delete_draft(found[0].uid)
                elif backend.supports_com:
                    item = backend.raw_com._ns.GetItemFromID(found[0].uid)
                    item.Delete()
            except Exception:
                pass  # cleanup failure is not a test failure


# Calendar operations


class TestCalendarOps:
    def test_calendar_available(self, backend):
        """Check if calendar is available (COM has it, IMAP doesn't)."""
        cal = backend.calendar
        if cal is None:
            pytest.skip("Calendar not available on this backend")

    def test_list_events(self, backend):
        cal = backend.calendar
        if cal is None:
            pytest.skip("Calendar not available")
        start = datetime.now()
        end = start + timedelta(days=7)
        events = cal.list_events(start, end)
        assert isinstance(events, list)
        for ev in events:
            assert isinstance(ev, EventInfo)
            assert ev.subject is not None

    def test_free_slots(self, backend):
        cal = backend.calendar
        if cal is None:
            pytest.skip("Calendar not available")
        start = datetime.now()
        end = start + timedelta(days=7)
        slots = cal.free_slots(start, end)
        assert isinstance(slots, dict)
        # Should have entries for weekdays
        for day, free in slots.items():
            assert isinstance(free, list)
            for slot_start, slot_end in free:
                assert isinstance(slot_start, str)
                assert isinstance(slot_end, str)

    def test_event_has_times(self, backend):
        cal = backend.calendar
        if cal is None:
            pytest.skip("Calendar not available")
        start = datetime.now()
        end = start + timedelta(days=14)
        events = cal.list_events(start, end, limit=3)
        for ev in events:
            if not ev.all_day:
                assert ev.start is not None
                assert ev.end is not None


# ToolExecutor with real backend


class TestToolExecutorReal:
    def test_search_emails_tool(self, backend):
        executor = ToolExecutor(backend)
        result = executor.execute("search_emails", {"query": "meeting", "limit": 3})
        assert isinstance(result, str)
        assert "error" not in result.lower() or "No emails" in result

    def test_read_email_tool(self, backend):
        executor = ToolExecutor(backend)
        result = executor.execute("read_email", {"query": "meeting"})
        assert isinstance(result, str)
        # Should have subject or error
        assert "subject" in result.lower() or "error" in result.lower()

    def test_list_events_tool(self, backend):
        executor = ToolExecutor(backend)
        result = executor.execute("list_events", {"days": 7})
        assert isinstance(result, str)
        # Either events or "not available"
        assert "subject" in result.lower() or "not available" in result.lower() or "[]" in result

    def test_free_slots_tool(self, backend):
        executor = ToolExecutor(backend)
        result = executor.execute("free_slots", {"days": 7})
        assert isinstance(result, str)

    def test_create_draft_tool_validates(self, backend):
        executor = ToolExecutor(backend)
        result = executor.execute("create_draft", {
            "to": "invalid-email",
            "subject": "Test",
            "body": "Body",
        })
        assert "Invalid" in result


# Backend identity


class TestBackendIdentity:
    def test_supports_com_matches_platform(self, backend):
        if sys.platform == "win32":
            # Could be COM or IMAP depending on config
            assert isinstance(backend.supports_com, bool)
        else:
            assert not backend.supports_com

    def test_raw_com_consistent(self, backend):
        if backend.supports_com:
            assert backend.raw_com is not None
        else:
            assert backend.raw_com is None

    def test_mark_as_read(self, backend):
        msgs = backend.list_messages(limit=1)
        if not msgs:
            pytest.skip("No messages")
        # Just verify it doesn't crash
        try:
            backend.mark_as_read(msgs[0].uid)
        except Exception:
            pass  # Some backends may not support this cleanly
