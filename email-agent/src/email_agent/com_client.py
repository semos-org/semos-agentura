"""Outlook COM automation client for emails and calendar.

Requires Windows with Outlook installed. Guarded by sys.platform check
in backend.py - this module is never imported on non-Windows systems.
"""

from __future__ import annotations

import logging
from datetime import datetime
from pathlib import Path

import win32com.client

from .exceptions import COMError

logger = logging.getLogger(__name__)

# Outlook item classes
OL_MAIL = 43
OL_APPOINTMENT = 26
OL_MEETING_REQUEST = 53

# Outlook folder IDs
OL_FOLDER_INBOX = 6
OL_FOLDER_SENT = 5
OL_FOLDER_DRAFTS = 16
OL_FOLDER_DELETED = 3
OL_FOLDER_CALENDAR = 9
OL_FOLDER_JUNK = 23


class OutlookCOM:
    """Wrapper around Outlook COM automation."""

    def __init__(self) -> None:
        try:
            self._app = win32com.client.Dispatch("Outlook.Application")
            self._ns = self._app.GetNamespace("MAPI")
        except Exception as e:
            raise COMError(f"Failed to connect to Outlook: {e}") from e
        logger.info("Outlook COM connected: %s v%s", self._app.Name, self._app.Version)

    # Email: Search

    def search_emails(
        self,
        query: str = "",
        folder_id: int = OL_FOLDER_INBOX,
        limit: int = 50,
        *,
        from_addr: str = "",
        to_addr: str = "",
        since: str = "",
        before: str = "",
        unread_only: bool = False,
        has_attachments: bool | None = None,
    ) -> list[dict]:
        """Search emails with composable filters. All filters are AND-combined.

        Args:
            query: Subject keyword (LIKE match). Optional.
            folder_id: Outlook folder constant.
            limit: Max results.
            from_addr: Sender email address (LIKE match).
            to_addr: Recipient email address (LIKE match).
            since: ISO date string (YYYY-MM-DD). Emails on or after this date.
            before: ISO date string (YYYY-MM-DD). Emails before this date.
            unread_only: Only return unread emails.
            has_attachments: Filter by attachment presence (True/False/None=any).
        """
        folder = self._ns.GetDefaultFolder(folder_id)
        clauses = []
        if query:
            clauses.append(f"\"urn:schemas:httpmail:subject\" LIKE '%{query}%'")
        if from_addr:
            clauses.append(f"\"urn:schemas:httpmail:fromemail\" LIKE '%{from_addr}%'")
        if to_addr:
            clauses.append(f"\"urn:schemas:httpmail:displayto\" LIKE '%{to_addr}%'")
        if since:
            clauses.append(f"\"urn:schemas:httpmail:datereceived\" >= '{since}'")
        if before:
            clauses.append(f"\"urn:schemas:httpmail:datereceived\" < '{before}'")
        if unread_only:
            clauses.append('"urn:schemas:httpmail:read" = 0')
        if has_attachments is True:
            clauses.append('"urn:schemas:httpmail:hasattachment" = 1')
        elif has_attachments is False:
            clauses.append('"urn:schemas:httpmail:hasattachment" = 0')

        if clauses:
            filt = "@SQL=" + " AND ".join(clauses)
            items = folder.Items.Restrict(filt)
        else:
            items = folder.Items
        items.Sort("[ReceivedTime]", True)

        results = []
        item = items.GetFirst()
        while item and len(results) < limit:
            try:
                if item.Class == OL_MAIL:
                    results.append(self._mail_to_dict(item))
            except Exception as e:
                logger.debug("Skipping item: %s", e)
            item = items.GetNext()

        desc = query or ",".join(f for f in [from_addr, to_addr, since, before] if f) or "all"
        logger.info("Search '%s': %d results", desc, len(results))
        return results

    def read_email(self, entry_id: str, save_attachments_to: str | None = None) -> dict:
        """Read a full email by its EntryID, optionally saving attachments."""
        item = self._ns.GetItemFromID(entry_id)
        result = self._mail_to_dict(item, include_body=True)

        if save_attachments_to and item.Attachments.Count > 0:
            att_dir = Path(save_attachments_to)
            att_dir.mkdir(parents=True, exist_ok=True)
            for i in range(item.Attachments.Count):
                att = item.Attachments.Item(i + 1)
                save_path = att_dir / att.FileName
                att.SaveAsFile(str(save_path))
                result["attachments"][i]["saved_path"] = str(save_path)
                logger.info("Saved attachment: %s", save_path)

        return result

    def _mail_to_dict(self, item, include_body: bool = False) -> dict:
        d = {
            "entry_id": item.EntryID,
            "subject": str(item.Subject or ""),
            "sender": str(getattr(item, "SenderName", "") or ""),
            "sender_email": str(getattr(item, "SenderEmailAddress", "") or ""),
            "to": str(getattr(item, "To", "") or ""),
            "cc": str(getattr(item, "CC", "") or ""),
            "received": str(item.ReceivedTime),
            "has_attachments": item.Attachments.Count > 0,
            "attachment_count": item.Attachments.Count,
            "attachments": [],
        }
        for i in range(item.Attachments.Count):
            att = item.Attachments.Item(i + 1)
            d["attachments"].append(
                {
                    "filename": att.FileName,
                    "size": att.Size,
                    "saved_path": None,
                }
            )
        if include_body:
            d["body"] = str(item.Body or "")
        return d

    # Email: Draft & Send

    def _compose(
        self, to: str, subject: str, body: str, cc: str = "", html: bool = False, attachments: list[str] | None = None
    ):
        """Create a MailItem with the given fields."""
        mail = self._app.CreateItem(0)
        mail.To = to.replace(",", ";")
        mail.Subject = subject
        if html:
            mail.HTMLBody = body
        else:
            mail.Body = body
        if cc:
            mail.CC = cc.replace(",", ";")
        for path in attachments or []:
            mail.Attachments.Add(str(Path(path).resolve()))
        return mail

    def create_draft(
        self, to: str, subject: str, body: str, cc: str = "", html: bool = False, attachments: list[str] | None = None
    ) -> str:
        """Create an email draft. Set html=True for HTML body. Returns EntryID."""
        mail = self._compose(to, subject, body, cc=cc, html=html, attachments=attachments)
        mail.Save()
        logger.info("Draft created: %s -> %s", subject, to)
        return mail.EntryID

    def send_email(
        self, to: str, subject: str, body: str, cc: str = "", html: bool = False, attachments: list[str] | None = None
    ) -> None:
        """Send an email immediately. Set html=True for HTML body."""
        mail = self._compose(to, subject, body, cc=cc, html=html, attachments=attachments)
        mail.Send()
        logger.info("Email sent: %s -> %s", subject, to)

    # Calendar: List Events

    def list_events(self, start: datetime, end: datetime, limit: int = 500) -> list[dict]:
        """List calendar events in a date range."""
        calendar = self._ns.GetDefaultFolder(OL_FOLDER_CALENDAR)
        items = calendar.Items
        items.IncludeRecurrences = True
        items.Sort("[Start]")

        filt = f"[Start] >= '{start.strftime('%d.%m.%Y %H:%M')}' AND [End] <= '{end.strftime('%d.%m.%Y 23:59')}'"
        restricted = items.Restrict(filt)

        events = []
        item = restricted.GetFirst()
        while item and len(events) < limit:
            try:
                ev_start = str(item.Start)
                if ev_start > end.strftime("%Y-%m-%d 23:59"):
                    break
                events.append(
                    {
                        "entry_id": item.EntryID,
                        "subject": str(item.Subject or ""),
                        "start": ev_start,
                        "end": str(item.End),
                        "location": str(getattr(item, "Location", "") or ""),
                        "all_day": bool(item.AllDayEvent),
                        "organizer": str(getattr(item, "Organizer", "") or ""),
                        "required": str(getattr(item, "RequiredAttendees", "") or ""),
                    }
                )
            except Exception as e:
                logger.debug("Skipping event: %s", e)
            try:
                item = restricted.GetNext()
            except Exception:
                break

        logger.info("Events %s - %s: %d found", start.strftime("%d.%m"), end.strftime("%d.%m.%Y"), len(events))
        return events

    def free_slots(
        self, start: datetime, end: datetime, work_start: int = 8, work_end: int = 17
    ) -> dict[str, list[tuple[str, str]]]:
        """Calculate free time slots per weekday from calendar events."""
        from datetime import timedelta

        events = self.list_events(start, end)

        by_date: dict[str, list[tuple[float, float]]] = {}
        for ev in events:
            ev_start = datetime.fromisoformat(ev["start"].replace("+00:00", ""))
            ev_end = datetime.fromisoformat(ev["end"].replace("+00:00", ""))
            date_key = ev_start.strftime("%Y-%m-%d")
            if date_key not in by_date:
                by_date[date_key] = []
            by_date[date_key].append(
                (
                    ev_start.hour + ev_start.minute / 60,
                    ev_end.hour + ev_end.minute / 60,
                )
            )

        result = {}
        current = start.replace(hour=0, minute=0, second=0)
        while current <= end:
            if current.weekday() < 5:
                date_key = current.strftime("%Y-%m-%d")
                label = current.strftime("%a %d.%m")
                busy = sorted(by_date.get(date_key, []))
                merged = []
                for s, e in busy:
                    s = max(s, work_start)
                    e = min(e, work_end)
                    if s >= e:
                        continue
                    if merged and s <= merged[-1][1]:
                        merged[-1] = (merged[-1][0], max(merged[-1][1], e))
                    else:
                        merged.append((s, e))
                free = []
                prev = work_start
                for s, e in merged:
                    if s > prev:
                        free.append((f"{int(prev)}:{int((prev % 1) * 60):02d}", f"{int(s)}:{int((s % 1) * 60):02d}"))
                    prev = e
                if prev < work_end:
                    free.append((f"{int(prev)}:{int((prev % 1) * 60):02d}", f"{work_end}:00"))
                result[label] = free
            current += timedelta(days=1)

        return result

    # Calendar: Create Event

    def create_event(
        self,
        subject: str,
        start: datetime,
        end: datetime,
        location: str = "",
        body: str = "",
        required_attendees: str = "",
    ) -> str:
        """Create a calendar event. Returns the EntryID."""
        appt = self._app.CreateItem(1)
        appt.Subject = subject
        appt.Start = start.strftime("%Y-%m-%d %H:%M")
        appt.End = end.strftime("%Y-%m-%d %H:%M")
        if location:
            appt.Location = location
        if body:
            appt.Body = body
        if required_attendees:
            appt.RequiredAttendees = required_attendees
            appt.MeetingStatus = 1
        appt.Save()
        logger.info("Event created: %s (%s - %s)", subject, start, end)
        return appt.EntryID

    # Folder iteration (for archive dump)

    def iter_folder(self, folder_id: int = OL_FOLDER_INBOX, oldest_first: bool = True):
        """Iterate all items in a folder, oldest first by default. Yields (item, dict)."""
        folder = self._ns.GetDefaultFolder(folder_id)
        items = folder.Items
        items.Sort("[ReceivedTime]", not oldest_first)

        item = items.GetFirst()
        while item:
            try:
                if item.Class == OL_MAIL:
                    yield item, self._mail_to_dict(item)
                elif item.Class in (OL_APPOINTMENT, OL_MEETING_REQUEST):
                    yield (
                        item,
                        {
                            "entry_id": item.EntryID,
                            "subject": str(item.Subject or ""),
                            "received": str(getattr(item, "ReceivedTime", "")),
                            "class": item.Class,
                        },
                    )
            except Exception as e:
                logger.debug("Skipping item in iteration: %s", e)
            item = items.GetNext()

    def iter_calendar(self, start: datetime, end: datetime):
        """Iterate calendar events in range. Yields dicts."""
        for ev in self.list_events(start, end, limit=100_000):
            yield ev
