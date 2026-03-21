from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime


@dataclass
class EventInfo:
    """Calendar event."""

    entry_id: str = ""
    subject: str = ""
    start: datetime | None = None
    end: datetime | None = None
    location: str = ""
    all_day: bool = False
    organizer: str = ""
    required_attendees: str = ""

    def __str__(self) -> str:
        s = self.start.strftime("%Y-%m-%d %H:%M") if self.start else "?"
        e = self.end.strftime("%H:%M") if self.end else "?"
        day = " [ALL DAY]" if self.all_day else ""
        loc = f" @ {self.location}" if self.location else ""
        return f"{s}-{e}  {self.subject}{loc}{day}"


@dataclass
class Attachment:
    """Email attachment."""

    filename: str
    content_type: str
    data: bytes


@dataclass
class EmailMessage:
    """Parsed email message."""

    uid: str
    message_id: str | None = None
    subject: str = ""
    sender: str = ""
    sender_name: str = ""
    to: list[str] = field(default_factory=list)
    cc: list[str] = field(default_factory=list)
    body_text: str = ""
    body_html: str = ""
    date: datetime | None = None
    attachments: list[Attachment] = field(default_factory=list)
    is_read: bool = False

    @property
    def body(self) -> str:
        """Return the best available body (prefer text, fall back to HTML)."""
        return self.body_text or self.body_html

    def __str__(self) -> str:
        date_str = self.date.strftime("%Y-%m-%d %H:%M") if self.date else "unknown"
        read_marker = " " if self.is_read else "*"
        return f"{read_marker} [{date_str}] {self.sender}: {self.subject}"
