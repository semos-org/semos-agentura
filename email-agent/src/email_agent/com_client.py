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
OL_FOLDER_TODO = 28


import re

_ANGLE_EMAIL_RE = re.compile(r"<([^>]+)>")


def _add_recipients(mail, raw: str, recipient_type: int = 1) -> None:
    """Add recipients to a MailItem via Recipients.Add().

    Parses 'Name <email>' or bare email from a semicolon-separated
    string. Calls Resolve() on each recipient so the SMTP address
    is fully set - required for new Outlook / Outlook online.
    """
    if not raw or not raw.strip():
        return
    for part in raw.split(";"):
        part = part.strip()
        if not part:
            continue
        m = _ANGLE_EMAIL_RE.search(part)
        email = m.group(1).strip() if m else part
        recip = mail.Recipients.Add(email)
        recip.Type = recipient_type
        recip.Resolve()


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
        flag_status: str = "",
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
            flag_status: Filter by flag: "marked" or "complete" (empty=any).
        """
        # FlagStatus: 0=NoFlag, 1=Complete, 2=Marked
        _FLAG_MAP = {"marked": 2, "complete": 1}
        flag_val = _FLAG_MAP.get(flag_status) if flag_status else None

        # Use To-Do folder for flag queries (pre-filtered by Outlook, fast)
        if flag_val is not None:
            folder = self._ns.GetDefaultFolder(OL_FOLDER_TODO)
        else:
            folder = self._ns.GetDefaultFolder(folder_id)

        dasl = []
        if query:
            q = query.replace("'", "''")
            dasl.append(f"\"urn:schemas:httpmail:subject\" LIKE '%{q}%'")
        if from_addr:
            dasl.append(f"\"urn:schemas:httpmail:fromemail\" LIKE '%{from_addr}%'")
        if to_addr:
            dasl.append(f"\"urn:schemas:httpmail:displayto\" LIKE '%{to_addr}%'")
        if since:
            dasl.append(f"\"urn:schemas:httpmail:datereceived\" >= '{since}'")
        if before:
            dasl.append(f"\"urn:schemas:httpmail:datereceived\" < '{before}'")
        if unread_only:
            dasl.append('"urn:schemas:httpmail:read" = 0')
        if has_attachments is True:
            dasl.append('"urn:schemas:httpmail:hasattachment" = 1')
        elif has_attachments is False:
            dasl.append('"urn:schemas:httpmail:hasattachment" = 0')

        items = folder.Items
        # JET filter first (To-Do folder: only mail items)
        if flag_val is not None:
            items = items.Restrict("[MessageClass] = 'IPM.Note'")
        # DASL filter for date/subject/sender
        if dasl:
            items = items.Restrict("@SQL=" + " AND ".join(dasl))

        sort_prop = "[LastModificationTime]" if flag_val is not None else "[ReceivedTime]"
        items.Sort(sort_prop, True)

        results = []
        item = items.GetFirst()
        while item and len(results) < limit:
            try:
                if item.Class == OL_MAIL:
                    # Post-filter flag value (To-Do has both marked + complete)
                    if flag_val is not None and getattr(item, "FlagStatus", 0) != flag_val:
                        item = items.GetNext()
                        continue
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
            seen_names: dict[str, int] = {}
            for i in range(item.Attachments.Count):
                att = item.Attachments.Item(i + 1)
                name = att.FileName
                # Deduplicate filenames (inline images often repeat)
                count = seen_names.get(name, 0)
                seen_names[name] = count + 1
                if count > 0:
                    stem = Path(name).stem
                    suffix = Path(name).suffix
                    name = f"{stem}_{count}{suffix}"
                save_path = att_dir / name
                try:
                    att.SaveAsFile(str(save_path))
                except OSError as e:
                    logger.warning(
                        "Cannot save attachment %s: %s (cloud/linked?)",
                        att.FileName,
                        e,
                    )
                    result["attachments"][i]["error"] = str(e)
                    continue
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
            "flag_status": getattr(item, "FlagStatus", 0),
            "has_attachments": item.Attachments.Count > 0,
            "attachment_count": item.Attachments.Count,
            "attachments": [],
        }
        PR_ATTACH_FLAGS = "http://schemas.microsoft.com/mapi/proptag/0x37140003"
        for i in range(item.Attachments.Count):
            att = item.Attachments.Item(i + 1)
            try:
                inline = att.PropertyAccessor.GetProperty(PR_ATTACH_FLAGS) == 4
            except Exception:
                inline = False
            d["attachments"].append(
                {
                    "filename": att.FileName,
                    "size": att.Size,
                    "inline": inline,
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
        # Use Recipients.Add() instead of .To = ... so Outlook
        # resolves display names for external addresses.
        _add_recipients(mail, to, recipient_type=1)  # olTo
        _add_recipients(mail, cc, recipient_type=2)  # olCC
        mail.Subject = subject
        if html:
            mail.HTMLBody = body
        else:
            mail.Body = body
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
        """Calculate free time slots per weekday.

        Uses event iteration (reads only Start/End per item). With scoped
        date ranges this is fast and always matches the actual calendar.
        """
        return self._free_slots_events(start, end, work_start, work_end)

    def _free_slots_freebusy(
        self, start: datetime, end: datetime, work_start: int, work_end: int
    ) -> dict[str, list[tuple[str, str]]]:
        """Fast path: Outlook FreeBusy API (requires Exchange)."""
        from datetime import timedelta

        slot_minutes = 15
        recip = self._ns.CreateRecipient(self._ns.CurrentUser.Address)
        recip.Resolve()
        if not recip.Resolved:
            raise RuntimeError("Could not resolve current user for FreeBusy")

        # FreeBusy(Start, MinPerChar, CompleteFormat)
        # Returns string: 0=free, 1=tentative, 2=busy, 3=OOF
        fb_start = start.replace(hour=0, minute=0, second=0, microsecond=0)
        fb = recip.FreeBusy(fb_start, slot_minutes, True)
        if not fb:
            raise RuntimeError("FreeBusy returned empty result")

        total_days = (end - fb_start).days + 1
        slots_per_day = (24 * 60) // slot_minutes

        result = {}
        for day_offset in range(total_days):
            current = fb_start + timedelta(days=day_offset)
            if current > end:
                break
            if current.weekday() >= 5:
                continue

            label = current.strftime("%a %d.%m")
            day_base = day_offset * slots_per_day
            ws = (work_start * 60) // slot_minutes
            we = (work_end * 60) // slot_minutes

            free = []
            free_start = None
            for i in range(ws, we):
                idx = day_base + i
                is_free = idx < len(fb) and fb[idx] == "0"
                if is_free and free_start is None:
                    free_start = i
                elif not is_free and free_start is not None:
                    free.append(self._slot_range(free_start, i, slot_minutes))
                    free_start = None
            if free_start is not None:
                free.append(self._slot_range(free_start, we, slot_minutes))

            result[label] = free

        logger.info("Free slots %s - %s (FreeBusy API)", start.strftime("%d.%m"), end.strftime("%d.%m.%Y"))
        return result

    def _free_slots_events(
        self, start: datetime, end: datetime, work_start: int, work_end: int
    ) -> dict[str, list[tuple[str, str]]]:
        """Slow path: iterate calendar events, only reading Start/End."""
        from datetime import timedelta

        calendar = self._ns.GetDefaultFolder(OL_FOLDER_CALENDAR)
        items = calendar.Items
        items.IncludeRecurrences = True
        items.Sort("[Start]")

        filt = f"[Start] >= '{start.strftime('%d.%m.%Y %H:%M')}' AND [Start] <= '{end.strftime('%d.%m.%Y 23:59')}'"
        restricted = items.Restrict(filt)

        by_date: dict[str, list[tuple[float, float]]] = {}
        item = restricted.GetFirst()
        count = 0
        while item and count < 500:
            try:
                ev_start = item.Start
                ev_end = item.End
                date_key = ev_start.strftime("%Y-%m-%d")
                if date_key not in by_date:
                    by_date[date_key] = []
                by_date[date_key].append(
                    (
                        ev_start.hour + ev_start.minute / 60,
                        ev_end.hour + ev_end.minute / 60,
                    )
                )
                count += 1
            except Exception:
                pass
            try:
                item = restricted.GetNext()
            except Exception:
                break

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

        logger.info(
            "Free slots %s - %s: %d events (fallback)", start.strftime("%d.%m"), end.strftime("%d.%m.%Y"), count
        )
        return result

    @staticmethod
    def _slot_range(start_slot: int, end_slot: int, minutes: int) -> tuple[str, str]:
        """Convert slot indices to time strings."""
        sm = start_slot * minutes
        em = end_slot * minutes
        return f"{sm // 60}:{sm % 60:02d}", f"{em // 60}:{em % 60:02d}"

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
        yield from self.list_events(start, end, limit=100_000)
