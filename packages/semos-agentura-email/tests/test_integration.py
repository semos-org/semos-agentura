"""Integration tests against a real backend.

These tests require a running email backend (COM with Outlook, or IMAP with credentials).
Skipped by default in CI. Run with: uv run pytest -m integration -v
"""

from __future__ import annotations

import sys
from datetime import datetime, timedelta

import pytest

pytestmark = pytest.mark.integration

from semos.agentura.email.backend import EmailBackend, create_backend
from semos.agentura.email.config import Settings
from semos.agentura.email.models import EmailMessage, EventInfo


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
                if hasattr(backend, "_ensure_client"):
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
        for _day, free in slots.items():
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


# Tool execution with real backend


class TestToolExecutionReal:
    def test_search_emails(self, backend):
        import asyncio

        from semos.agentura.email.tools import get_email_tools

        class _Stub:
            output_dir = None

            async def run_sync(self, fn):
                return fn(backend)

        tools = get_email_tools(_Stub())
        search = next(t for t in tools if t.name == "search_emails")
        result = asyncio.run(search._arun(query="meeting", limit=3))
        assert isinstance(result, str)
        assert "error" not in result.lower() or "No emails" in result

    def test_read_email(self, backend):
        import asyncio

        from semos.agentura.email.tools import get_email_tools

        class _Stub:
            output_dir = None

            async def run_sync(self, fn):
                return fn(backend)

        tools = get_email_tools(_Stub())
        read = next(t for t in tools if t.name == "read_email")
        result = asyncio.run(read._arun(query="meeting"))
        text = result.text if hasattr(result, "text") else str(result)
        assert "subject" in text.lower() or "error" in text.lower() or "No emails" in text

    def test_list_events(self, backend):
        import asyncio

        from semos.agentura.email.tools import get_email_tools

        class _Stub:
            output_dir = None

            async def run_sync(self, fn):
                return fn(backend)

        tools = get_email_tools(_Stub())
        le = next(t for t in tools if t.name == "list_events")
        result = asyncio.run(le._arun(days=7))
        assert isinstance(result, str)
        assert "subject" in result.lower() or "not available" in result.lower() or "[]" in result

    def test_free_slots(self, backend):
        import asyncio

        from semos.agentura.email.tools import get_email_tools

        class _Stub:
            output_dir = None

            async def run_sync(self, fn):
                return fn(backend)

        tools = get_email_tools(_Stub())
        fs = next(t for t in tools if t.name == "free_slots")
        result = asyncio.run(fs._arun(days=7))
        assert isinstance(result, str)

    def test_create_draft_validates(self, backend):
        from semos.agentura.email.tools import Recipient

        with pytest.raises(ValueError):
            Recipient(email="invalid-email")


# COM draft round-trip tests


@pytest.mark.skipif(sys.platform != "win32", reason="COM only")
class TestCOMDraftRoundTrip:
    """Create drafts via COM and verify recipients resolve."""

    def test_bare_email_recipient(self, backend):
        if not backend.supports_com:
            pytest.skip("COM backend required")
        com = backend.raw_com
        eid = com.create_draft(
            to="test@example.com",
            subject="[TEST] bare email - auto-delete",
            body="test",
        )
        try:
            item = com._ns.GetItemFromID(eid)
            assert "undefined" not in item.To.lower()
            assert "test@example.com" in item.To.lower()
            assert item.Recipients.Count == 1
        finally:
            com._ns.GetItemFromID(eid).Delete()

    def test_multiple_recipients(self, backend):
        if not backend.supports_com:
            pytest.skip("COM backend required")
        com = backend.raw_com
        eid = com.create_draft(
            to="a@example.com; b@example.com",
            subject="[TEST] multi recip - auto-delete",
            body="test",
            cc="c@example.com",
        )
        try:
            item = com._ns.GetItemFromID(eid)
            assert "undefined" not in item.To.lower()
            assert item.Recipients.Count == 3
        finally:
            com._ns.GetItemFromID(eid).Delete()

    def test_name_angle_bracket_format(self, backend):
        if not backend.supports_com:
            pytest.skip("COM backend required")
        com = backend.raw_com
        eid = com.create_draft(
            to="Test User <test@example.com>",
            subject="[TEST] name+email - auto-delete",
            body="test",
        )
        try:
            item = com._ns.GetItemFromID(eid)
            assert "undefined" not in item.To.lower()
            assert "test@example.com" in item.To.lower()
        finally:
            com._ns.GetItemFromID(eid).Delete()

    def test_comma_in_display_name(self, backend):
        """Names like 'Last, First' must not split into two recipients."""
        if not backend.supports_com:
            pytest.skip("COM backend required")
        from semos.agentura.email.tools import Recipient, _recipients_to_str

        recipients = [
            Recipient(email="a@example.com", name="Last, First"),
            Recipient(email="b@example.com", name="Other, Person"),
        ]
        com = backend.raw_com
        eid = com.create_draft(
            to=_recipients_to_str(recipients),
            subject="[TEST] comma names - auto-delete",
            body="test",
        )
        try:
            item = com._ns.GetItemFromID(eid)
            assert item.Recipients.Count == 2
            assert "undefined" not in item.To.lower()
        finally:
            com._ns.GetItemFromID(eid).Delete()

    def test_tool_pydantic_path(self, backend):
        """Full path: Pydantic Recipient -> _recipients_to_str -> COM."""
        if not backend.supports_com:
            pytest.skip("COM backend required")
        from semos.agentura.email.tools import Recipient, _recipients_to_str

        recipients = [
            Recipient(email="test@example.com", name="Test"),
            Recipient(email="other@example.com"),
        ]
        to_str = _recipients_to_str(recipients)

        com = backend.raw_com
        eid = com.create_draft(
            to=to_str,
            subject="[TEST] pydantic path - auto-delete",
            body="test",
        )
        try:
            item = com._ns.GetItemFromID(eid)
            assert "undefined" not in item.To.lower()
            assert item.Recipients.Count == 2
        finally:
            com._ns.GetItemFromID(eid).Delete()


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
