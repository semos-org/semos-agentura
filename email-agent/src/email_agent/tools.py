"""Tool definitions for LLM function calling with email backend.

Contains:
- TOOL_DEFINITIONS: JSON schemas for the mailgent LLM function calling
- ToolExecutor: backend dispatch for tool execution
- AgentTool subclasses: MCP tool wrappers that delegate to service._exec()
- get_email_tools(): factory to create all AgentTool instances bound to a service
"""

from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timedelta
from typing import Any

from agentura_commons import AgentTool, FileAttachment
from pydantic import BaseModel, Field

from .formatting import md_to_plain

logger = logging.getLogger(__name__)

# Basic email validation: local@domain.tld
_EMAIL_RE = re.compile(r"^[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}$")
# RFC 5322: "Display Name" <email> or bare email
_RFC5322_RE = re.compile(r'^"?([^"<]*)"?\s*<([^>]+)>$')


def _parse_date_range(args: dict) -> tuple[datetime, datetime]:
    """Parse start/end/days from tool args into a (start, end) datetime pair."""
    start_str = args.get("start", "")
    end_str = args.get("end", "")
    start = datetime.strptime(start_str, "%Y-%m-%d") if start_str else datetime.now()
    if end_str:
        end = datetime.strptime(end_str, "%Y-%m-%d").replace(hour=23, minute=59)
    else:
        days = int(args.get("days", 14))
        end = start + timedelta(days=days)
    return start, end


def _validate_email_list(raw: str) -> tuple[str, list[str]]:
    """Parse and validate a list of email addresses.

    Accepts semicolon- or comma-separated addresses in RFC 5322 format
    ("Display Name" <email>) or bare email. Preserves display names.
    Returns (cleaned semicolon-separated string, list of errors).
    """
    if not raw or not raw.strip():
        return "", []
    parts = re.split(r"[;,]+", raw)
    clean = []
    errors = []
    for p in parts:
        entry = p.strip()
        if not entry:
            continue
        m = _RFC5322_RE.match(entry)
        if m:
            name, addr = m.group(1).strip(), m.group(2).strip()
            if _EMAIL_RE.match(addr):
                if name:
                    clean.append(f'"{name}" <{addr}>')
                else:
                    clean.append(addr)
            else:
                errors.append(f"Invalid email: '{addr}'")
        else:
            addr = entry.strip("<>").strip()
            if _EMAIL_RE.match(addr):
                clean.append(addr)
            else:
                errors.append(f"Invalid email: '{entry}'")
    return "; ".join(clean), errors


TOOL_DEFINITIONS = [
    {
        "type": "function",
        "function": {
            "name": "search_emails",
            "description": "Search emails by subject keyword. Returns list of matching emails.",
            "parameters": {
                "type": "object",
                "required": ["query"],
                "properties": {
                    "query": {"type": "string", "description": "Search term to find in email subjects"},
                    "limit": {"type": "integer", "description": "Max results (default 20)", "default": 20},
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_email",
            "description": "Read the full content of the most recent email matching a query.",
            "parameters": {
                "type": "object",
                "required": ["query"],
                "properties": {
                    "query": {"type": "string", "description": "Search term to find the email"},
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_events",
            "description": "List calendar events for the next N days.",
            "parameters": {
                "type": "object",
                "properties": {
                    "days": {"type": "integer", "description": "Number of days ahead (default 14)", "default": 14},
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "free_slots",
            "description": "Calculate free meeting slots for the next N weekdays between work hours (8:00-17:00).",
            "parameters": {
                "type": "object",
                "properties": {
                    "days": {"type": "integer", "description": "Number of days ahead (default 14)", "default": 14},
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "create_draft",
            "description": "Create an email draft.",
            "parameters": {
                "type": "object",
                "required": ["to", "subject", "body"],
                "properties": {
                    "to": {
                        "type": "string",
                        "description": ('Recipients as "Display Name" <email>; semicolon-separated for multiple'),
                    },
                    "subject": {"type": "string", "description": "Email subject line"},
                    "body": {"type": "string", "description": "Email body text"},
                    "cc": {
                        "type": "string",
                        "description": ('CC as "Display Name" <email>; semicolon-separated for multiple'),
                        "default": "",
                    },
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "draft_event",
            "description": (
                "Create a calendar event draft. If attendees are specified, meeting invitations are NOT sent yet."
            ),
            "parameters": {
                "type": "object",
                "required": ["subject", "start", "end"],
                "properties": {
                    "subject": {"type": "string", "description": "Event title"},
                    "start": {"type": "string", "description": "Start time as 'YYYY-MM-DD HH:MM'"},
                    "end": {"type": "string", "description": "End time as 'YYYY-MM-DD HH:MM'"},
                    "location": {"type": "string", "description": "Event location", "default": ""},
                    "body": {"type": "string", "description": "Meeting body/agenda text", "default": ""},
                    "attendees": {
                        "type": "string",
                        "description": (
                            'Required attendees as "Display Name" <email>; semicolon-separated for multiple'
                        ),
                        "default": "",
                    },
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "send_event",
            "description": "Create a calendar event and immediately send meeting invitations.",
            "parameters": {
                "type": "object",
                "required": ["subject", "start", "end", "attendees"],
                "properties": {
                    "subject": {"type": "string", "description": "Event title"},
                    "start": {"type": "string", "description": "Start time as 'YYYY-MM-DD HH:MM'"},
                    "end": {"type": "string", "description": "End time as 'YYYY-MM-DD HH:MM'"},
                    "location": {"type": "string", "description": "Event location", "default": ""},
                    "body": {"type": "string", "description": "Meeting body/agenda text", "default": ""},
                    "attendees": {
                        "type": "string",
                        "description": (
                            'Required attendees as "Display Name" <email>; semicolon-separated for multiple'
                        ),
                    },
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "draft_reply",
            "description": "Create a reply draft to the most recent email matching a query.",
            "parameters": {
                "type": "object",
                "required": ["query", "body"],
                "properties": {
                    "query": {"type": "string", "description": "Search term to find the email to reply to"},
                    "body": {"type": "string", "description": "Reply body text"},
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "send_reply",
            "description": "Reply to the most recent email matching a query and send immediately.",
            "parameters": {
                "type": "object",
                "required": ["query", "body"],
                "properties": {
                    "query": {"type": "string", "description": "Search term to find the email to reply to"},
                    "body": {"type": "string", "description": "Reply body text"},
                },
            },
        },
    },
]


class ToolExecutor:
    """Executes tool calls from the LLM using an EmailBackend."""

    def __init__(self, backend) -> None:
        self._backend = backend

    def execute(self, name: str, arguments: dict) -> str:
        """Execute a tool and return the result as a JSON string."""
        try:
            result = self._dispatch(name, arguments)
            return json.dumps(result, default=str, ensure_ascii=False)
        except Exception as e:
            logger.error("Tool %s failed: %s", name, e)
            return json.dumps({"error": str(e)})

    def _dispatch(self, name: str, args: dict):
        match name:
            case "search_emails":
                msgs = self._backend.search_emails(
                    args.get("query", ""),
                    limit=args.get("limit", 20),
                    from_addr=args.get("from_addr", ""),
                    to_addr=args.get("to_addr", ""),
                    since=args.get("since", ""),
                    before=args.get("before", ""),
                    unread_only=args.get("unread_only", False),
                    has_attachments=args.get("has_attachments"),
                )
                return [
                    {
                        "entry_id": m.uid,
                        "subject": m.subject,
                        "sender": m.sender_name or m.sender,
                        "sender_email": m.sender,
                        "received": str(m.date or ""),
                        "has_attachments": bool(m.attachments),
                    }
                    for m in msgs
                ]

            case "read_email":
                # Resolve the email to read
                entry_id = args.get("entry_id", "")
                if entry_id:
                    uid = entry_id
                else:
                    msgs = self._backend.search_emails(
                        args.get("query", ""),
                        limit=1,
                        from_addr=args.get("from_addr", ""),
                        to_addr=args.get("to_addr", ""),
                    )
                    if not msgs:
                        return {"error": "No emails found matching the given criteria"}
                    uid = msgs[0].uid

                include_attachments = args.get("include_attachments", False)
                save_dir = args.get("_attachment_dir")

                # For COM backend: save attachments if requested
                if include_attachments and save_dir and self._backend.supports_com:
                    raw = self._backend.raw_com.read_email(
                        uid,
                        save_attachments_to=save_dir,
                    )
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
                    full = self._backend.get_message(uid)
                    att_info = [{"filename": a.filename} for a in full.attachments]

                return {
                    "entry_id": full.uid,
                    "subject": full.subject,
                    "sender": full.sender_name or full.sender,
                    "sender_email": full.sender,
                    "to": "; ".join(full.to),
                    "cc": "; ".join(full.cc),
                    "body": full.body,
                    "attachments": att_info,
                }

            case "list_events":
                cal = self._backend.calendar
                if cal is None:
                    return {"error": "Calendar not available with the current backend"}
                start, end = _parse_date_range(args)
                events = cal.list_events(start, end)
                return [
                    {
                        "subject": e.subject,
                        "start": str(e.start or ""),
                        "end": str(e.end or ""),
                        "location": e.location,
                        "all_day": e.all_day,
                        "organizer": e.organizer,
                    }
                    for e in events
                ]

            case "free_slots":
                cal = self._backend.calendar
                if cal is None:
                    return {"error": "Calendar not available with the current backend"}
                start, end = _parse_date_range(args)
                return cal.free_slots(start, end)

            case "create_draft":
                to_raw = args.get("to", "")
                if isinstance(to_raw, list):
                    to_raw = "; ".join(to_raw)
                to, errs = _validate_email_list(str(to_raw))
                if errs:
                    return {"error": errs}
                if not to:
                    return {"error": "No valid recipients"}
                cc_raw = args.get("cc", "")
                if isinstance(cc_raw, list):
                    cc_raw = "; ".join(cc_raw)
                cc, cc_errs = _validate_email_list(str(cc_raw))
                if cc_errs:
                    return {"error": cc_errs}
                att = args.get("attachments")
                if isinstance(att, str):
                    att = [a.strip() for a in att.split(",") if a.strip()] or None
                entry_id = self._backend.create_draft(
                    to=to,
                    subject=args["subject"],
                    body=md_to_plain(args["body"]),
                    cc=cc,
                    attachments=att,
                )
                return {"status": "draft created", "entry_id": entry_id}

            case "draft_event" | "send_event":
                cal = self._backend.calendar
                if cal is None:
                    return {"error": "Calendar not available with the current backend"}
                start = datetime.strptime(args["start"], "%Y-%m-%d %H:%M")
                end = datetime.strptime(args["end"], "%Y-%m-%d %H:%M")
                attendees = args.get("attendees", "")
                if attendees:
                    attendees, errs = _validate_email_list(attendees)
                    if errs:
                        return {"error": errs}
                body = md_to_plain(args.get("body", ""))

                # For COM backend with attendees: use the meeting creation
                # helper that handles Display() vs Send()
                if self._backend.supports_com and attendees:
                    send = name == "send_event"
                    entry_id = self._create_meeting_com(
                        subject=args["subject"],
                        start=start,
                        end=end,
                        location=args.get("location", ""),
                        body=body,
                        attendees=attendees,
                        send=send,
                    )
                else:
                    entry_id = cal.create_event(
                        subject=args["subject"],
                        start=start,
                        end=end,
                        location=args.get("location", ""),
                        body=body,
                        required_attendees=attendees,
                    )
                status = (
                    "event sent (invitations delivered)"
                    if name == "send_event"
                    else "event draft created (invitations NOT sent)"
                )
                return {"status": status, "entry_id": entry_id}

            case "draft_reply" | "send_reply":
                msgs = self._backend.search_emails(
                    args.get("query", ""),
                    limit=1,
                    from_addr=args.get("from_addr", ""),
                    to_addr=args.get("to_addr", ""),
                )
                if not msgs:
                    return {"error": "No emails found matching the given criteria"}
                uid = msgs[0].uid
                body_text = md_to_plain(args["body"])
                if name == "send_reply":
                    self._backend.send_reply(uid, body_text)
                    status = "reply sent"
                else:
                    self._backend.draft_reply(uid, body_text)
                    status = "reply draft created"
                return {
                    "status": status,
                    "to": msgs[0].sender_name or msgs[0].sender,
                    "subject": msgs[0].subject,
                }

            case _:
                return {"error": f"Unknown tool: {name}"}

    def _create_meeting_com(
        self,
        subject: str,
        start: datetime,
        end: datetime,
        location: str = "",
        body: str = "",
        attendees: str = "",
        send: bool = False,
    ) -> str:
        """COM-specific meeting creation with Display() for drafts."""
        com = self._backend.raw_com
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
            logger.info("Meeting opened for review: %s attendees=%s", subject, attendees)
        else:
            appt.Save()
            logger.info("Appointment saved: %s", subject)
        return getattr(appt, "EntryID", "")


# AgentTool input models


class SearchEmailsInput(BaseModel):
    query: str = Field(
        default="",
        description="Subject keyword (partial match). Optional.",
    )
    limit: int = Field(
        default=20,
        description="Max results to return.",
    )
    from_addr: str = Field(
        default="",
        description="Sender email address (partial match).",
    )
    to_addr: str = Field(
        default="",
        description="Recipient email address (partial match).",
    )
    since: str = Field(
        default="",
        description="Only emails on or after this date (YYYY-MM-DD).",
    )
    before: str = Field(
        default="",
        description="Only emails before this date (YYYY-MM-DD).",
    )
    unread_only: bool = Field(
        default=False,
        description="Only return unread emails.",
    )
    has_attachments: bool | None = Field(
        default=None,
        description="true = only with attachments, false = only without.",
    )


class ReadEmailInput(BaseModel):
    entry_id: str = Field(
        default="",
        description="Exact email ID from search_emails results. Preferred.",
    )
    query: str = Field(
        default="",
        description="Subject keyword (partial match). Used if no entry_id.",
    )
    from_addr: str = Field(
        default="",
        description="Sender email address (partial match).",
    )
    to_addr: str = Field(
        default="",
        description="Recipient email address (partial match).",
    )
    include_attachments: bool = Field(
        default=False,
        description="If true, save attachments and return download URLs.",
    )


class ListEventsInput(BaseModel):
    start: str = Field(
        default="",
        description="Start date (YYYY-MM-DD). Defaults to today.",
    )
    end: str = Field(
        default="",
        description="End date (YYYY-MM-DD). If set, days is ignored.",
    )
    days: int = Field(
        default=14,
        description="Number of days from start. Only used when end is not set.",
    )


class FreeSlotsInput(BaseModel):
    start: str = Field(
        default="",
        description="Start date (YYYY-MM-DD). Defaults to today.",
    )
    end: str = Field(
        default="",
        description="End date (YYYY-MM-DD). If set, days is ignored.",
    )
    days: int = Field(
        default=14,
        description="Number of days from start. Only used when end is not set.",
    )


class CreateDraftInput(BaseModel):
    to: str = Field(
        description="Recipient email address(es), semicolon-separated.",
    )
    subject: str = Field(
        description="Email subject line.",
    )
    body: str = Field(
        description="Email body text.",
    )
    cc: str = Field(
        default="",
        description="CC recipients, semicolon-separated.",
    )
    attachments: list[FileAttachment] | None = Field(
        default=None,
        description=(
            "Array of file objects with 'name' and 'content' fields. "
            'Example: [{"name": "report.docx", "content": "/path/to/file.docx"}]. '
            "The content field accepts a file path, base64, or data URI."
        ),
    )


class DraftEventInput(BaseModel):
    subject: str = Field(
        description="Event title.",
    )
    start: str = Field(
        description="Start time as 'YYYY-MM-DD HH:MM'.",
    )
    end: str = Field(
        description="End time as 'YYYY-MM-DD HH:MM'.",
    )
    location: str = Field(
        default="",
        description="Event location.",
    )
    body: str = Field(
        default="",
        description="Meeting body/agenda text.",
    )
    attendees: str = Field(
        default="",
        description="Required attendees (semicolon-separated emails).",
    )


class SendEventInput(BaseModel):
    subject: str = Field(
        description="Event title.",
    )
    start: str = Field(
        description="Start time as 'YYYY-MM-DD HH:MM'.",
    )
    end: str = Field(
        description="End time as 'YYYY-MM-DD HH:MM'.",
    )
    attendees: str = Field(
        description="Required attendees (semicolon-separated emails).",
    )
    location: str = Field(
        default="",
        description="Event location.",
    )
    body: str = Field(
        default="",
        description="Meeting body/agenda text.",
    )


class DraftReplyInput(BaseModel):
    query: str = Field(
        description="Search term to find the email to reply to.",
    )
    body: str = Field(
        description="Reply body text.",
    )


class SendReplyInput(BaseModel):
    query: str = Field(
        description="Search term to find the email to reply to.",
    )
    body: str = Field(
        description="Reply body text.",
    )


# AgentTool implementations


class SearchEmailsTool(AgentTool):
    name: str = "search_emails"
    description: str = (
        "Search emails with composable filters: subject, sender, recipient, "
        "date range, unread, attachments. All optional, AND-combined."
    )
    args_schema: type[BaseModel] = SearchEmailsInput
    read_only: bool = True
    idempotent: bool = True

    async def _arun(self, **kwargs: Any) -> str:
        return await self._service._exec(
            "search_emails",
            {
                "query": kwargs.get("query", ""),
                "limit": kwargs.get("limit", 20),
                "from_addr": kwargs.get("from_addr", ""),
                "to_addr": kwargs.get("to_addr", ""),
                "since": kwargs.get("since", ""),
                "before": kwargs.get("before", ""),
                "unread_only": kwargs.get("unread_only", False),
                "has_attachments": kwargs.get("has_attachments"),
            },
        )


class ReadEmailTool(AgentTool):
    name: str = "read_email"
    description: str = "Read the full content of the most recent email matching filters (subject, sender, recipient)."
    args_schema: type[BaseModel] = ReadEmailInput
    read_only: bool = True
    idempotent: bool = True

    async def _arun(self, **kwargs: Any) -> str:
        return await self._service._read_email(**kwargs)


class ListEventsTool(AgentTool):
    name: str = "list_events"
    description: str = (
        "List calendar events. Specify start/end dates (YYYY-MM-DD) for an "
        "exact range, or days for a relative range from today."
    )
    args_schema: type[BaseModel] = ListEventsInput
    read_only: bool = True
    idempotent: bool = True

    async def _arun(self, **kwargs: Any) -> str:
        return await self._service._exec(
            "list_events",
            {
                "start": kwargs.get("start", ""),
                "end": kwargs.get("end", ""),
                "days": kwargs.get("days", 14),
            },
        )


class FreeSlotsTool(AgentTool):
    name: str = "free_slots"
    description: str = (
        "Calculate free meeting slots during business hours. Specify start/end "
        "dates (YYYY-MM-DD) for an exact range, or days for a relative range "
        "from today. Preferred over list_events when looking for available time."
    )
    args_schema: type[BaseModel] = FreeSlotsInput
    read_only: bool = True
    idempotent: bool = True

    async def _arun(self, **kwargs: Any) -> str:
        return await self._service._exec(
            "free_slots",
            {
                "start": kwargs.get("start", ""),
                "end": kwargs.get("end", ""),
                "days": kwargs.get("days", 14),
            },
        )


class CreateDraftTool(AgentTool):
    name: str = "create_draft"
    description: str = (
        "Create an email draft with optional attachments. Accepts absolute file paths or base64-encoded content."
    )
    args_schema: type[BaseModel] = CreateDraftInput

    async def _arun(self, **kwargs: Any) -> str:
        return await self._service._create_draft(**kwargs)


class DraftEventTool(AgentTool):
    name: str = "draft_event"
    description: str = "Create a calendar event draft (invitations NOT sent)."
    args_schema: type[BaseModel] = DraftEventInput

    async def _arun(self, **kwargs: Any) -> str:
        return await self._service._exec(
            "draft_event",
            {
                "subject": kwargs["subject"],
                "start": kwargs["start"],
                "end": kwargs["end"],
                "location": kwargs.get("location", ""),
                "body": kwargs.get("body", ""),
                "attendees": kwargs.get("attendees", ""),
            },
        )


class SendEventTool(AgentTool):
    name: str = "send_event"
    description: str = "Create a calendar event and send invitations immediately."
    args_schema: type[BaseModel] = SendEventInput
    destructive: bool = True

    async def _arun(self, **kwargs: Any) -> str:
        return await self._service._exec(
            "send_event",
            {
                "subject": kwargs["subject"],
                "start": kwargs["start"],
                "end": kwargs["end"],
                "location": kwargs.get("location", ""),
                "body": kwargs.get("body", ""),
                "attendees": kwargs["attendees"],
            },
        )


class DraftReplyTool(AgentTool):
    name: str = "draft_reply"
    description: str = "Create a reply draft to the most recent email matching a query."
    args_schema: type[BaseModel] = DraftReplyInput

    async def _arun(self, **kwargs: Any) -> str:
        return await self._service._exec(
            "draft_reply",
            {"query": kwargs["query"], "body": kwargs["body"]},
        )


class SendReplyTool(AgentTool):
    name: str = "send_reply"
    description: str = "Reply to the most recent email matching a query and send immediately."
    args_schema: type[BaseModel] = SendReplyInput
    destructive: bool = True

    async def _arun(self, **kwargs: Any) -> str:
        return await self._service._exec(
            "send_reply",
            {"query": kwargs["query"], "body": kwargs["body"]},
        )


def get_email_tools(service: Any) -> list[AgentTool]:
    """Create all email-agent tools bound to a service instance."""
    tools = [
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
