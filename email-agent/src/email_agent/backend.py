"""Backend abstraction - EmailBackend and CalendarBackend protocols with implementations."""

from __future__ import annotations

import logging
import sys
from datetime import datetime
from typing import Protocol, runtime_checkable

from .config import Settings
from .exceptions import BackendNotAvailable, CalendarNotSupported
from .models import EmailMessage, EventInfo

logger = logging.getLogger(__name__)


# Protocols


@runtime_checkable
class CalendarBackend(Protocol):
    """Protocol for calendar operations."""

    def list_events(self, start: datetime, end: datetime, limit: int = 500) -> list[EventInfo]: ...
    def free_slots(
        self, start: datetime, end: datetime, work_start: int = 8, work_end: int = 17,
    ) -> dict[str, list[tuple[str, str]]]: ...
    def create_event(
        self, subject: str, start: datetime, end: datetime,
        location: str = "", body: str = "", required_attendees: str = "",
    ) -> str: ...


@runtime_checkable
class EmailBackend(Protocol):
    """Protocol for email operations."""

    # Connection
    def connect(self) -> None: ...
    def disconnect(self) -> None: ...

    # Read
    def list_messages(self, folder: str = "INBOX", limit: int = 25) -> list[EmailMessage]: ...
    def get_message(self, uid: str) -> EmailMessage: ...
    def search_emails(
        self, query: str = "", folder: str = "INBOX", limit: int = 50, *,
        from_addr: str = "", to_addr: str = "",
        since: str = "", before: str = "", unread_only: bool = False,
        has_attachments: bool | None = None,
    ) -> list[EmailMessage]: ...

    # Send/Draft
    def send_email(
        self, to: str, subject: str, body: str,
        cc: str = "", html: bool = False, attachments: list[str] | None = None,
    ) -> None: ...
    def create_draft(
        self, to: str, subject: str, body: str,
        cc: str = "", html: bool = False, attachments: list[str] | None = None,
    ) -> str: ...
    def list_drafts(self, limit: int = 25) -> list[EmailMessage]: ...

    # Reply
    def draft_reply(self, uid: str, body: str) -> str: ...
    def send_reply(self, uid: str, body: str) -> None: ...

    # Flags
    def mark_as_read(self, uid: str) -> None: ...

    # Calendar
    @property
    def calendar(self) -> CalendarBackend | None: ...

    # Backend identity
    @property
    def supports_com(self) -> bool: ...
    @property
    def raw_com(self) -> object | None: ...


# COM dict to typed model helpers


def _parse_com_datetime(s: str) -> datetime | None:
    """Parse a COM datetime string to a datetime object."""
    if not s:
        return None
    try:
        # COM returns "YYYY-MM-DD HH:MM:SS+00:00" style strings
        return datetime.fromisoformat(s.replace("+00:00", ""))
    except (ValueError, AttributeError):
        return None


def _com_dict_to_email(d: dict) -> EmailMessage:
    """Convert an OutlookCOM mail dict to an EmailMessage."""
    from .models import Attachment
    return EmailMessage(
        uid=d["entry_id"],
        subject=d.get("subject", ""),
        sender=d.get("sender_email", ""),
        sender_name=d.get("sender", ""),
        to=[a.strip() for a in d.get("to", "").split(";") if a.strip()],
        cc=[a.strip() for a in d.get("cc", "").split(";") if a.strip()],
        body_text=d.get("body", ""),
        date=_parse_com_datetime(d.get("received", "")),
        is_read=True,
        attachments=[
            Attachment(filename=a["filename"], content_type="application/octet-stream", data=b"")
            for a in d.get("attachments", [])
        ],
    )


def _com_dict_to_event(d: dict) -> EventInfo:
    """Convert an OutlookCOM event dict to an EventInfo."""
    return EventInfo(
        entry_id=d.get("entry_id", ""),
        subject=d.get("subject", ""),
        start=_parse_com_datetime(d.get("start", "")),
        end=_parse_com_datetime(d.get("end", "")),
        location=d.get("location", ""),
        all_day=d.get("all_day", False),
        organizer=d.get("organizer", ""),
        required_attendees=d.get("required", ""),
    )


# IMAP Backend


class IMAPBackend:
    """Email backend using IMAP/SMTP (via existing MailClient)."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._client = None  # lazy init
        self._calendar_backend: CalendarBackend | None = None

    def _ensure_client(self):
        if self._client is None:
            from .client import MailClient
            self._client = MailClient(self._settings)
        return self._client

    def connect(self) -> None:
        self._ensure_client().connect()

    def disconnect(self) -> None:
        if self._client:
            self._client.disconnect()

    # Read

    def list_messages(self, folder: str = "INBOX", limit: int = 25) -> list[EmailMessage]:
        return self._ensure_client().fetch_messages(folder=folder, limit=limit)

    def get_message(self, uid: str) -> EmailMessage:
        return self._ensure_client().get_message(uid)

    def search_emails(
        self, query: str = "", folder: str = "INBOX", limit: int = 50, *,
        from_addr: str = "", to_addr: str = "",
        since: str = "", before: str = "", unread_only: bool = False,
        has_attachments: bool | None = None,
    ) -> list[EmailMessage]:
        """Search with composable filters via IMAP SEARCH."""
        from datetime import datetime as _dt
        client = self._ensure_client()
        since_dt = _dt.strptime(since, "%Y-%m-%d") if since else None
        before_dt = _dt.strptime(before, "%Y-%m-%d") if before else None
        uids = client.search(
            folder=folder,
            subject=query or None,
            from_addr=from_addr or None,
            since=since_dt,
            before=before_dt,
            unseen=unread_only,
        )
        uids = uids[:limit]
        messages = []
        for uid in uids:
            try:
                msg = client.get_message(uid)
                # Post-filter: to_addr and has_attachments (IMAP doesn't support these natively)
                if to_addr and to_addr.lower() not in " ".join(msg.to).lower():
                    continue
                if has_attachments is True and not msg.attachments:
                    continue
                if has_attachments is False and msg.attachments:
                    continue
                messages.append(msg)
            except Exception:
                pass
        return messages

    # Send/Draft

    def send_email(
        self, to: str, subject: str, body: str,
        cc: str = "", html: bool = False, attachments: list[str] | None = None,
    ) -> None:
        from pathlib import Path
        to_list = [a.strip() for a in to.replace(",", ";").split(";") if a.strip()]
        cc_list = [a.strip() for a in cc.replace(",", ";").split(";") if a.strip()] if cc else None
        att_paths = [Path(p) for p in attachments] if attachments else None
        self._ensure_client().send(
            to=to_list, subject=subject, body=body,
            body_type="html" if html else "plain",
            cc=cc_list, attachments=att_paths,
        )

    def create_draft(
        self, to: str, subject: str, body: str,
        cc: str = "", html: bool = False, attachments: list[str] | None = None,
    ) -> str:
        from pathlib import Path
        to_list = [a.strip() for a in to.replace(",", ";").split(";") if a.strip()]
        cc_list = [a.strip() for a in cc.replace(",", ";").split(";") if a.strip()] if cc else None
        att_paths = [Path(p) for p in attachments] if attachments else None
        self._ensure_client().save_draft(
            to=to_list, subject=subject, body=body,
            body_type="html" if html else "plain",
            cc=cc_list, attachments=att_paths,
        )
        return ""  # IMAP APPEND doesn't reliably return UID

    def list_drafts(self, limit: int = 25) -> list[EmailMessage]:
        return self._ensure_client().list_drafts(limit=limit)

    # Reply

    def draft_reply(self, uid: str, body: str) -> str:
        """Create a reply draft via IMAP - compose new message with In-Reply-To."""
        client = self._ensure_client()
        original = client.get_message(uid)
        subject = original.subject
        if not subject.lower().startswith("re:"):
            subject = f"Re: {subject}"

        # Build reply body with quoted original
        quoted = "\n".join(f"> {line}" for line in original.body.splitlines())
        reply_body = f"{body}\n\n{quoted}"

        # Compose and save as draft with reply headers
        from email.mime.multipart import MIMEMultipart
        from email.mime.text import MIMEText

        msg = MIMEMultipart("mixed")
        msg["From"] = self._settings.email_address or ""
        msg["To"] = original.sender
        msg["Subject"] = subject
        if original.message_id:
            msg["In-Reply-To"] = original.message_id
            msg["References"] = original.message_id
        msg.attach(MIMEText(reply_body, "plain"))

        # Append to drafts
        imap = client._imap
        drafts_folder = imap._find_folder("\\Drafts")
        raw = msg.as_bytes()
        status, _ = imap._imap.append(drafts_folder, "\\Draft", None, raw)
        return ""

    def send_reply(self, uid: str, body: str) -> None:
        """Send a reply via SMTP."""
        client = self._ensure_client()
        original = client.get_message(uid)
        subject = original.subject
        if not subject.lower().startswith("re:"):
            subject = f"Re: {subject}"

        quoted = "\n".join(f"> {line}" for line in original.body.splitlines())
        reply_body = f"{body}\n\n{quoted}"
        client.send(to=[original.sender], subject=subject, body=reply_body, body_type="plain")

    # Flags

    def mark_as_read(self, uid: str) -> None:
        self._ensure_client().mark_as_read(uid)

    # Calendar

    @property
    def calendar(self) -> CalendarBackend | None:
        return self._calendar_backend

    @calendar.setter
    def calendar(self, cal: CalendarBackend | None) -> None:
        self._calendar_backend = cal

    # Identity

    @property
    def supports_com(self) -> bool:
        return False

    @property
    def raw_com(self) -> object | None:
        return None


# COM Backend

if sys.platform == "win32":

    class COMCalendar:
        """Calendar backend using Outlook COM."""

        def __init__(self, com: object) -> None:
            self._com = com

        def list_events(self, start: datetime, end: datetime, limit: int = 500) -> list[EventInfo]:
            raw = self._com.list_events(start, end, limit=limit)
            return [_com_dict_to_event(d) for d in raw]

        def free_slots(
            self, start: datetime, end: datetime, work_start: int = 8, work_end: int = 17,
        ) -> dict[str, list[tuple[str, str]]]:
            return self._com.free_slots(start, end, work_start=work_start, work_end=work_end)

        def create_event(
            self, subject: str, start: datetime, end: datetime,
            location: str = "", body: str = "", required_attendees: str = "",
        ) -> str:
            return self._com.create_event(
                subject, start, end, location=location,
                body=body, required_attendees=required_attendees,
            )

    class COMBackend:
        """Email + Calendar backend using Outlook COM."""

        def __init__(self, settings: Settings) -> None:
            from .com_client import OutlookCOM
            self._settings = settings
            self._com = OutlookCOM()
            self._calendar = COMCalendar(self._com)

        def connect(self) -> None:
            pass  # COM connects in __init__

        def disconnect(self) -> None:
            pass

        # Read

        def list_messages(self, folder: str = "INBOX", limit: int = 25) -> list[EmailMessage]:
            from .com_client import OL_FOLDER_INBOX, OL_FOLDER_DRAFTS, OL_FOLDER_SENT, OL_MAIL
            folder_map = {
                "INBOX": OL_FOLDER_INBOX, "Inbox": OL_FOLDER_INBOX,
                "Drafts": OL_FOLDER_DRAFTS, "DRAFTS": OL_FOLDER_DRAFTS,
                "Sent": OL_FOLDER_SENT, "SENT": OL_FOLDER_SENT,
            }
            fid = folder_map.get(folder, OL_FOLDER_INBOX)
            com_folder = self._com._ns.GetDefaultFolder(fid)
            items = com_folder.Items
            items.Sort("[ReceivedTime]", True)

            messages = []
            item = items.GetFirst()
            while item and len(messages) < limit:
                try:
                    if item.Class == OL_MAIL:
                        messages.append(_com_dict_to_email(self._com._mail_to_dict(item)))
                except Exception:
                    pass
                item = items.GetNext()
            return messages

        def get_message(self, uid: str) -> EmailMessage:
            raw = self._com.read_email(uid)
            return _com_dict_to_email(raw)

        def search_emails(
            self, query: str = "", folder: str = "INBOX", limit: int = 50, *,
            from_addr: str = "", to_addr: str = "",
            since: str = "", before: str = "", unread_only: bool = False,
            has_attachments: bool | None = None,
        ) -> list[EmailMessage]:
            from .com_client import OL_FOLDER_INBOX, OL_FOLDER_DRAFTS, OL_FOLDER_SENT
            folder_map = {
                "INBOX": OL_FOLDER_INBOX, "Inbox": OL_FOLDER_INBOX,
                "Drafts": OL_FOLDER_DRAFTS, "Sent": OL_FOLDER_SENT,
            }
            fid = folder_map.get(folder, OL_FOLDER_INBOX)
            raw = self._com.search_emails(
                query, folder_id=fid, limit=limit,
                from_addr=from_addr, to_addr=to_addr,
                since=since, before=before,
                unread_only=unread_only, has_attachments=has_attachments,
            )
            return [_com_dict_to_email(d) for d in raw]

        # Send/Draft

        def send_email(
            self, to: str, subject: str, body: str,
            cc: str = "", html: bool = False, attachments: list[str] | None = None,
        ) -> None:
            self._com.send_email(to, subject, body, cc=cc, html=html, attachments=attachments)

        def create_draft(
            self, to: str, subject: str, body: str,
            cc: str = "", html: bool = False, attachments: list[str] | None = None,
        ) -> str:
            return self._com.create_draft(to, subject, body, cc=cc, html=html, attachments=attachments)

        def list_drafts(self, limit: int = 25) -> list[EmailMessage]:
            return self.list_messages(folder="Drafts", limit=limit)

        # Reply

        def draft_reply(self, uid: str, body: str) -> str:
            item = self._com._ns.GetItemFromID(uid)
            reply = item.Reply()
            existing = str(reply.Body or "")
            reply.Body = body + "\n\n" + existing
            reply.Save()
            return reply.EntryID

        def send_reply(self, uid: str, body: str) -> None:
            item = self._com._ns.GetItemFromID(uid)
            reply = item.Reply()
            existing = str(reply.Body or "")
            reply.Body = body + "\n\n" + existing
            reply.Send()

        # Flags

        def mark_as_read(self, uid: str) -> None:
            item = self._com._ns.GetItemFromID(uid)
            item.UnRead = False
            item.Save()

        # Calendar

        @property
        def calendar(self) -> CalendarBackend:
            return self._calendar

        # Identity

        @property
        def supports_com(self) -> bool:
            return True

        @property
        def raw_com(self):
            return self._com


# Factory


def create_backend(settings: Settings | None = None) -> EmailBackend:
    """Create the appropriate email backend based on settings."""
    settings = settings or Settings()
    kind = settings.detected_backend

    if kind == "imap":
        backend = IMAPBackend(settings)
        # Attach CalDAV calendar if configured
        if settings.caldav_url:
            try:
                from .caldav_client import CalDAVCalendar
                backend.calendar = CalDAVCalendar(settings)
            except ImportError:
                logger.warning("caldav library not installed - calendar unavailable")
        return backend

    elif kind == "com":
        if sys.platform != "win32":
            raise BackendNotAvailable("COM backend requires Windows")
        return COMBackend(settings)

    elif kind == "graph":
        raise BackendNotAvailable("Graph API backend not yet implemented (Phase 3)")

    else:
        raise BackendNotAvailable(f"Unknown backend: {kind}")
