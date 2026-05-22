"""Email-agent tool definitions.

All tools are AgentTool subclasses with Pydantic input models.
Each tool's _arun() contains its business logic directly.
Backend calls are routed through service.run_sync() for COM thread safety.
"""

from __future__ import annotations

import json
import logging
import shutil
import uuid as _uuid
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

from agentura_commons import AgentTool, FileAttachment
from agentura_commons.base import NamedFile, ToolResult
from pydantic import BaseModel, EmailStr, Field

from .formatting import md_to_plain

logger = logging.getLogger(__name__)


# Pydantic types


class Recipient(BaseModel):
    """Email recipient: email (required), name (optional)."""

    email: EmailStr = Field(description="Email address")
    name: str = Field(default="", description="Display name")

    def __str__(self) -> str:
        if self.name:
            return f"{self.name} <{self.email}>"
        return self.email


def _recipients_to_str(recipients: list[Recipient]) -> str:
    """Join recipients into semicolon-separated string for Outlook COM."""
    return "; ".join(str(r) for r in recipients)


# Pydantic input models


class SearchEmailsInput(BaseModel):
    query: str = Field(default="", description="Subject keyword (partial match).")
    limit: int = Field(default=20, ge=1, le=200, description="Max results.")
    from_addr: str = Field(default="", description="Sender email (partial match).")
    to_addr: str = Field(default="", description="Recipient email (partial match).")
    since: date | None = Field(default=None, description="Only emails on or after this date.")
    before: date | None = Field(default=None, description="Only emails before this date.")
    unread_only: bool = Field(default=False, description="Only return unread emails.")
    has_attachments: bool | None = Field(
        default=None,
        description="true = only with, false = only without.",
    )


class ReadEmailInput(BaseModel):
    entry_id: str = Field(
        default="",
        description="Exact email ID from search_emails. Preferred.",
    )
    query: str = Field(
        default="",
        description="Subject keyword. Used if no entry_id.",
    )
    from_addr: str = Field(default="", description="Sender email (partial match).")
    to_addr: str = Field(default="", description="Recipient email (partial match).")
    include_attachments: bool = Field(
        default=False,
        description="Save attachments and return download URLs.",
    )


class ListEventsInput(BaseModel):
    start: date | None = Field(default=None, description="Start date. Defaults to today.")
    end: date | None = Field(default=None, description="End date. If set, days is ignored.")
    days: int = Field(
        default=14,
        ge=1,
        le=90,
        description="Days from start. Only used when end is not set.",
    )


class FreeSlotsInput(BaseModel):
    start: date | None = Field(default=None, description="Start date. Defaults to today.")
    end: date | None = Field(default=None, description="End date. If set, days is ignored.")
    days: int = Field(
        default=14,
        ge=1,
        le=90,
        description="Days from start. Only used when end is not set.",
    )


class CreateDraftInput(BaseModel):
    to: list[Recipient] = Field(
        description="Recipients as [{name, email}].",
    )
    subject: str = Field(description="Email subject line.")
    body: str = Field(description="Email body text.")
    cc: list[Recipient] = Field(
        default=[],
        description="CC recipients as [{name, email}].",
    )
    attachments: list[FileAttachment] | None = Field(
        default=None,
        description=("File attachments with 'name' and 'content' fields. Content: file path, base64, or data URI."),
    )


class EventInput(BaseModel):
    """Base for calendar event tools."""

    subject: str = Field(description="Event title.")
    start: datetime = Field(description="Start time (YYYY-MM-DDTHH:MM).")
    end: datetime = Field(description="End time (YYYY-MM-DDTHH:MM).")
    location: str = Field(default="", description="Event location.")
    body: str = Field(default="", description="Meeting body/agenda text.")


class DraftEventInput(EventInput):
    attendees: list[Recipient] = Field(
        default=[],
        description="Attendees as [{name, email}]. NOT sent.",
    )


class SendEventInput(EventInput):
    attendees: list[Recipient] = Field(
        min_length=1,
        description="Attendees as [{name, email}]. Sent immediately.",
    )


class ReplyInput(BaseModel):
    query: str = Field(description="Search term to find the email to reply to.")
    body: str = Field(description="Reply body text.")


# Date range helper


def _resolve_date_range(start: date | None, end: date | None, days: int = 14) -> tuple[datetime, datetime]:
    """Convert optional date/days into (start_dt, end_dt) datetimes."""
    start_dt = datetime.combine(start, datetime.min.time()) if start else datetime.now()
    if end:
        end_dt = datetime.combine(end, datetime.max.time().replace(microsecond=0))
    else:
        end_dt = start_dt + timedelta(days=days)
    return start_dt, end_dt


# AgentTool implementations


class SearchEmailsTool(AgentTool):
    name: str = "search_emails"
    description: str = (
        "Search emails with composable filters: subject, sender, "
        "recipient, date range, unread, attachments. "
        "All optional, AND-combined."
    )
    args_schema: type[BaseModel] = SearchEmailsInput
    read_only: bool = True
    idempotent: bool = True

    async def _arun(self, **kwargs: Any) -> str:
        inp = SearchEmailsInput(**kwargs)
        since = inp.since.isoformat() if inp.since else ""
        before = inp.before.isoformat() if inp.before else ""

        def do(backend):
            return backend.search_emails(
                inp.query,
                limit=inp.limit,
                from_addr=inp.from_addr,
                to_addr=inp.to_addr,
                since=since,
                before=before,
                unread_only=inp.unread_only,
                has_attachments=inp.has_attachments,
            )

        msgs = await self._service.run_sync(do)
        return json.dumps(
            [
                {
                    "entry_id": m.uid,
                    "subject": m.subject,
                    "sender": m.sender_name or m.sender,
                    "sender_email": m.sender,
                    "received": str(m.date or ""),
                    "has_attachments": bool(m.attachments),
                }
                for m in msgs
            ],
            default=str,
            ensure_ascii=False,
        )


class ReadEmailTool(AgentTool):
    name: str = "read_email"
    description: str = (
        "Read the full content of an email. Use entry_id from "
        "search_emails for exact lookup, or filters to find "
        "the most recent match."
    )
    args_schema: type[BaseModel] = ReadEmailInput
    read_only: bool = True
    idempotent: bool = True

    async def _arun(self, **kwargs: Any) -> ToolResult:
        inp = ReadEmailInput(**kwargs)

        att_dir = None
        if inp.include_attachments and self._service.output_dir:
            att_dir = str(self._service.output_dir / f"_att_{_uuid.uuid4().hex[:8]}")

        def do(backend):
            # Resolve email ID
            if inp.entry_id:
                uid = inp.entry_id
            else:
                msgs = backend.search_emails(
                    inp.query,
                    limit=1,
                    from_addr=inp.from_addr,
                    to_addr=inp.to_addr,
                )
                if not msgs:
                    return None, None, None
                uid = msgs[0].uid

            if inp.include_attachments and att_dir and backend.supports_com:
                raw = backend.raw_com.read_email(uid, save_attachments_to=att_dir)
                from .backend import _com_dict_to_email

                full = _com_dict_to_email(raw)
                att_info = [
                    {
                        "filename": a.get("filename", ""),
                        "size": a.get("size", 0),
                        "saved_path": a.get("saved_path"),
                    }
                    for a in raw.get("attachments", [])
                ]
            else:
                full = backend.get_message(uid)
                att_info = [{"filename": a.filename} for a in full.attachments]
            return uid, full, att_info

        uid, full, att_info = await self._service.run_sync(do)
        if full is None:
            return ToolResult(text="No emails found matching the given criteria")

        result = {
            "entry_id": full.uid,
            "subject": full.subject,
            "sender": full.sender_name or full.sender,
            "sender_email": full.sender,
            "to": "; ".join(full.to),
            "cc": "; ".join(full.cc),
            "body": full.body,
            "attachments": att_info,
        }

        # Move saved attachments to output_dir
        files: list[NamedFile] = []
        if inp.include_attachments and self._service.output_dir:
            for att in result.get("attachments", []):
                saved = att.pop("saved_path", None)
                if saved:
                    saved_p = Path(saved)
                    safe = f"{_uuid.uuid4().hex[:8]}_{saved_p.name}"
                    dest = self._service.output_dir / safe
                    shutil.move(str(saved_p), str(dest))
                    att["file"] = saved_p.name
                    att["size_bytes"] = dest.stat().st_size
                    files.append(NamedFile(path=dest, name=saved_p.name))

        return ToolResult(data=result, files=files)


class ListEventsTool(AgentTool):
    name: str = "list_events"
    description: str = (
        "List calendar events. Specify start/end dates for an exact range, or days for a relative range from today."
    )
    args_schema: type[BaseModel] = ListEventsInput
    read_only: bool = True
    idempotent: bool = True

    async def _arun(self, **kwargs: Any) -> str:
        inp = ListEventsInput(**kwargs)
        start_dt, end_dt = _resolve_date_range(inp.start, inp.end, inp.days)

        def do(backend):
            cal = backend.calendar
            if cal is None:
                return None
            return cal.list_events(start_dt, end_dt)

        events = await self._service.run_sync(do)
        if events is None:
            return json.dumps({"error": "Calendar not available"})
        return json.dumps(
            [
                {
                    "subject": e.subject,
                    "start": str(e.start or ""),
                    "end": str(e.end or ""),
                    "location": e.location,
                    "all_day": e.all_day,
                    "organizer": e.organizer,
                }
                for e in events
            ],
            default=str,
            ensure_ascii=False,
        )


class FreeSlotsTool(AgentTool):
    name: str = "free_slots"
    description: str = (
        "Calculate free meeting slots during business hours. "
        "Specify start/end dates for an exact range, or days "
        "for a relative range from today. "
        "Preferred over list_events for available time."
    )
    args_schema: type[BaseModel] = FreeSlotsInput
    read_only: bool = True
    idempotent: bool = True

    async def _arun(self, **kwargs: Any) -> str:
        inp = FreeSlotsInput(**kwargs)
        start_dt, end_dt = _resolve_date_range(inp.start, inp.end, inp.days)

        def do(backend):
            cal = backend.calendar
            if cal is None:
                return None
            return cal.free_slots(start_dt, end_dt)

        result = await self._service.run_sync(do)
        if result is None:
            return json.dumps({"error": "Calendar not available"})
        return json.dumps(result, default=str, ensure_ascii=False)


class CreateDraftTool(AgentTool):
    name: str = "create_draft"
    description: str = "Create an email draft with optional attachments."
    args_schema: type[BaseModel] = CreateDraftInput

    async def _arun(self, **kwargs: Any) -> str:
        inp = CreateDraftInput(**kwargs)

        # Resolve file attachments (async-safe, no COM)
        att_paths: list[str] = []
        for item in inp.attachments or []:
            name = item.get("name", "")
            content = item.get("content", name)
            ext = Path(name).suffix if name else ".bin"
            resolved = self._service.resolve_file(content, default_ext=ext, filename=name)
            logger.info("Resolved attachment: %s -> %s", name, resolved)
            att_paths.append(str(resolved))

        to_str = _recipients_to_str(inp.to)
        cc_str = _recipients_to_str(inp.cc)
        body = md_to_plain(inp.body)

        def do(backend):
            return backend.create_draft(
                to=to_str,
                subject=inp.subject,
                body=body,
                cc=cc_str,
                attachments=att_paths or None,
            )

        entry_id = await self._service.run_sync(do)
        return json.dumps({"status": "draft created", "entry_id": entry_id})


class DraftEventTool(AgentTool):
    name: str = "draft_event"
    description: str = "Create a calendar event draft (invitations NOT sent)."
    args_schema: type[BaseModel] = DraftEventInput

    async def _arun(self, **kwargs: Any) -> str:
        inp = DraftEventInput(**kwargs)
        attendees = _recipients_to_str(inp.attendees)
        body = md_to_plain(inp.body)

        def do(backend):
            if backend.supports_com and attendees:
                return _create_meeting_com(
                    backend.raw_com,
                    subject=inp.subject,
                    start=inp.start,
                    end=inp.end,
                    location=inp.location,
                    body=body,
                    attendees=attendees,
                    send=False,
                )
            cal = backend.calendar
            if cal is None:
                return None
            return cal.create_event(
                subject=inp.subject,
                start=inp.start,
                end=inp.end,
                location=inp.location,
                body=body,
                required_attendees=attendees,
            )

        entry_id = await self._service.run_sync(do)
        if entry_id is None:
            return json.dumps({"error": "Calendar not available"})
        return json.dumps(
            {
                "status": "event draft created (invitations NOT sent)",
                "entry_id": entry_id,
            }
        )


class SendEventTool(AgentTool):
    name: str = "send_event"
    description: str = "Create a calendar event and send invitations immediately."
    args_schema: type[BaseModel] = SendEventInput
    destructive: bool = True

    async def _arun(self, **kwargs: Any) -> str:
        inp = SendEventInput(**kwargs)
        attendees = _recipients_to_str(inp.attendees)
        body = md_to_plain(inp.body)

        def do(backend):
            if backend.supports_com:
                return _create_meeting_com(
                    backend.raw_com,
                    subject=inp.subject,
                    start=inp.start,
                    end=inp.end,
                    location=inp.location,
                    body=body,
                    attendees=attendees,
                    send=True,
                )
            cal = backend.calendar
            if cal is None:
                return None
            return cal.create_event(
                subject=inp.subject,
                start=inp.start,
                end=inp.end,
                location=inp.location,
                body=body,
                required_attendees=attendees,
            )

        entry_id = await self._service.run_sync(do)
        if entry_id is None:
            return json.dumps({"error": "Calendar not available"})
        return json.dumps(
            {
                "status": "event sent (invitations delivered)",
                "entry_id": entry_id,
            }
        )


class DraftReplyTool(AgentTool):
    name: str = "draft_reply"
    description: str = "Create a reply draft to the most recent email matching a query."
    args_schema: type[BaseModel] = ReplyInput

    async def _arun(self, **kwargs: Any) -> str:
        inp = ReplyInput(**kwargs)
        body = md_to_plain(inp.body)

        def do(backend):
            msgs = backend.search_emails(inp.query, limit=1)
            if not msgs:
                return {"error": "No emails found"}
            uid = msgs[0].uid
            backend.draft_reply(uid, body)
            return {
                "status": "reply draft created",
                "to": msgs[0].sender_name or msgs[0].sender,
                "subject": msgs[0].subject,
            }

        return json.dumps(await self._service.run_sync(do))


class SendReplyTool(AgentTool):
    name: str = "send_reply"
    description: str = "Reply to the most recent email matching a query and send immediately."
    args_schema: type[BaseModel] = ReplyInput
    destructive: bool = True

    async def _arun(self, **kwargs: Any) -> str:
        inp = ReplyInput(**kwargs)
        body = md_to_plain(inp.body)

        def do(backend):
            msgs = backend.search_emails(inp.query, limit=1)
            if not msgs:
                return {"error": "No emails found"}
            uid = msgs[0].uid
            backend.send_reply(uid, body)
            return {
                "status": "reply sent",
                "to": msgs[0].sender_name or msgs[0].sender,
                "subject": msgs[0].subject,
            }

        return json.dumps(await self._service.run_sync(do))


# COM meeting helper


def _create_meeting_com(
    com,
    subject: str,
    start: datetime,
    end: datetime,
    location: str = "",
    body: str = "",
    attendees: str = "",
    send: bool = False,
) -> str:
    """COM-specific meeting creation with Display() for drafts."""
    appt = com._app.CreateItem(1)
    appt.Subject = subject
    appt.Start = start.strftime("%Y-%m-%d %H:%M")
    appt.End = end.strftime("%Y-%m-%d %H:%M")
    if location:
        appt.Location = location
    if body:
        appt.Body = body
    if attendees:
        appt.RequiredAttendees = attendees
        appt.MeetingStatus = 1

    if send and attendees:
        appt.Send()
        logger.info("Meeting sent: %s to %s", subject, attendees)
    elif attendees:
        appt.Display(False)
        logger.info(
            "Meeting opened for review: %s attendees=%s",
            subject,
            attendees,
        )
    else:
        appt.Save()
        logger.info("Appointment saved: %s", subject)
    return getattr(appt, "EntryID", "")


# Tool factory


def get_email_tools(service: Any) -> list[AgentTool]:
    """Create all email-agent tools bound to a service instance."""
    tools: list[AgentTool] = [
        SearchEmailsTool(),
        ReadEmailTool(),
        ListEventsTool(),
        FreeSlotsTool(),
        CreateDraftTool(),
        DraftEventTool(),
        SendEventTool(),
        DraftReplyTool(),
        SendReplyTool(),
    ]
    for t in tools:
        t.bind_service(service)
    return tools
