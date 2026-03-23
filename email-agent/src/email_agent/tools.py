"""Tool definitions for LLM function calling with email backend."""

from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timedelta

from .exceptions import CalendarNotSupported
from .formatting import md_to_plain

logger = logging.getLogger(__name__)

# Basic email validation: local@domain.tld
_EMAIL_RE = re.compile(r"^[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}$")


def _validate_email_list(raw: str) -> tuple[str, list[str]]:
    """Parse and validate a list of email addresses.

    Accepts semicolon- or comma-separated addresses.
    Returns (cleaned semicolon-separated string, list of errors).
    """
    if not raw or not raw.strip():
        return "", []
    parts = re.split(r"[;,]+", raw)
    clean = []
    errors = []
    for p in parts:
        addr = p.strip()
        if not addr:
            continue
        # Extract email from "Name <email>" format
        m = re.search(r"<([^>]+)>", addr)
        if m:
            addr = m.group(1).strip()
        else:
            addr = addr.strip("<>").strip()
        if _EMAIL_RE.match(addr):
            clean.append(addr)
        else:
            errors.append(f"Invalid email: '{addr}'")
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
                    "to": {"type": "string", "description": "Recipient email address(es)"},
                    "subject": {"type": "string", "description": "Email subject line"},
                    "body": {"type": "string", "description": "Email body text"},
                    "cc": {"type": "string", "description": "CC recipients", "default": ""},
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "draft_event",
            "description": (
                "Create a calendar event draft. If attendees are specified, "
                "meeting invitations are NOT sent yet."
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
                        "description": "Required attendees (semicolon-separated emails)",
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
                    "attendees": {"type": "string", "description": "Required attendees (semicolon-separated emails)"},
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
                msgs = self._backend.search_emails(args["query"], limit=args.get("limit", 20))
                return [
                    {
                        "entry_id": m.uid, "subject": m.subject,
                        "sender": m.sender_name or m.sender,
                        "sender_email": m.sender,
                        "received": str(m.date or ""),
                        "has_attachments": bool(m.attachments),
                    }
                    for m in msgs
                ]

            case "read_email":
                msgs = self._backend.search_emails(args["query"], limit=1)
                if not msgs:
                    return {"error": f"No emails found matching '{args['query']}'"}
                full = self._backend.get_message(msgs[0].uid)
                return {
                    "entry_id": full.uid, "subject": full.subject,
                    "sender": full.sender_name or full.sender,
                    "sender_email": full.sender,
                    "to": "; ".join(full.to), "cc": "; ".join(full.cc),
                    "body": full.body,
                    "attachments": [{"filename": a.filename} for a in full.attachments],
                }

            case "list_events":
                cal = self._backend.calendar
                if cal is None:
                    return {"error": "Calendar not available with the current backend"}
                days = args.get("days", 14)
                start = datetime.now()
                end = start + timedelta(days=days)
                events = cal.list_events(start, end)
                return [
                    {
                        "subject": e.subject,
                        "start": str(e.start or ""), "end": str(e.end or ""),
                        "location": e.location, "all_day": e.all_day,
                        "organizer": e.organizer,
                    }
                    for e in events
                ]

            case "free_slots":
                cal = self._backend.calendar
                if cal is None:
                    return {"error": "Calendar not available with the current backend"}
                days = args.get("days", 14)
                start = datetime.now()
                end = start + timedelta(days=days)
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
                    to=to, subject=args["subject"],
                    body=md_to_plain(args["body"]), cc=cc,
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
                        subject=args["subject"], start=start, end=end,
                        location=args.get("location", ""), body=body,
                        attendees=attendees, send=send,
                    )
                else:
                    entry_id = cal.create_event(
                        subject=args["subject"], start=start, end=end,
                        location=args.get("location", ""), body=body,
                        required_attendees=attendees,
                    )
                status = (
                    "event sent (invitations delivered)" if name == "send_event"
                    else "event draft created (invitations NOT sent)"
                )
                return {"status": status, "entry_id": entry_id}

            case "draft_reply" | "send_reply":
                msgs = self._backend.search_emails(args["query"], limit=1)
                if not msgs:
                    return {"error": f"No emails found matching '{args['query']}'"}
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
        self, subject: str, start: datetime, end: datetime,
        location: str = "", body: str = "",
        attendees: str = "", send: bool = False,
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
