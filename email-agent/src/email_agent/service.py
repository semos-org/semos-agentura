"""MCP + A2A service wrapper for email-agent.

Usage:
    uvicorn email_agent.service:app --port 8001
"""

from __future__ import annotations

import asyncio
import logging
import os
import queue
import threading
from typing import Any

from agentura_commons import BaseAgentService, SkillDef, ToolDef, create_app

from .backend import create_backend
from .config import Settings
from .tools import ToolExecutor

logger = logging.getLogger(__name__)


class _COMWorker:
    """Dedicated single thread for all Outlook COM operations.

    COM objects are apartment-threaded on Windows - they can only be used
    from the thread that created them. This worker creates the backend once
    and processes all tool calls sequentially on that thread.
    """

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._queue: queue.Queue[tuple[str, dict, asyncio.Future]] = queue.Queue()
        self._thread = threading.Thread(target=self._run, daemon=True, name="com-worker")
        self._thread.start()

    def _run(self) -> None:
        """Worker loop - runs on the dedicated COM thread."""
        backend = create_backend(self._settings)
        backend.connect()
        executor = ToolExecutor(backend)
        logger.info("COM worker thread started")

        while True:
            tool_name, args, future = self._queue.get()
            try:
                result = executor.execute(tool_name, args)
                loop = future.get_loop()
                loop.call_soon_threadsafe(self._safe_set_result, future, result)
            except Exception as e:
                loop = future.get_loop()
                loop.call_soon_threadsafe(self._safe_set_exception, future, e)

    @staticmethod
    def _safe_set_result(future: asyncio.Future, result: Any) -> None:
        if not future.done():
            future.set_result(result)

    @staticmethod
    def _safe_set_exception(future: asyncio.Future, exc: Exception) -> None:
        if not future.done():
            future.set_exception(exc)

    async def execute(self, tool_name: str, args: dict) -> str:
        """Submit a tool call and await the result."""
        future: asyncio.Future[str] = asyncio.get_running_loop().create_future()
        self._queue.put((tool_name, args, future))
        return await future


class EmailAgentService(BaseAgentService):
    """Exposes email-agent's tools via MCP and skills via A2A."""

    def __init__(self) -> None:
        self._settings = Settings()
        self._worker: _COMWorker | None = None

    def _ensure_worker(self) -> _COMWorker:
        """Lazily start the COM worker thread on first use."""
        if self._worker is None:
            self._worker = _COMWorker(self._settings)
        return self._worker

    @property
    def agent_name(self) -> str:
        return "Email Agent"

    @property
    def agent_description(self) -> str:
        return "Email and calendar operations - search, read, send, draft, reply, and manage events."

    @property
    def agent_version(self) -> str:
        return "0.2.0"

    def get_tools(self) -> list[ToolDef]:
        return [
            ToolDef(name="search_emails", description="Search emails by subject keyword.", fn=self._search_emails),
            ToolDef(name="read_email", description="Read the full content of the most recent email matching a query.", fn=self._read_email),
            ToolDef(name="list_events", description="List calendar events for the next N days.", fn=self._list_events),
            ToolDef(name="free_slots", description="Calculate free meeting slots for the next N weekdays.", fn=self._free_slots),
            ToolDef(name="create_draft", description="Create an email draft.", fn=self._create_draft),
            ToolDef(name="draft_event", description="Create a calendar event draft (invitations NOT sent).", fn=self._draft_event),
            ToolDef(name="send_event", description="Create a calendar event and send invitations immediately.", fn=self._send_event),
            ToolDef(name="draft_reply", description="Create a reply draft to the most recent email matching a query.", fn=self._draft_reply),
            ToolDef(name="send_reply", description="Reply to the most recent email matching a query and send immediately.", fn=self._send_reply),
        ]

    def get_skills(self) -> list[SkillDef]:
        return [
            SkillDef(
                id="email-operations",
                name="Email Operations",
                description="Search, read, send, and draft emails and calendar events.",
                tags=["email", "calendar", "outlook"],
            ),
        ]

    async def execute_skill(self, skill_id: str, message: str, *, task_id: str | None = None) -> str:
        msg = message.lower()
        if "search" in msg:
            return await self._search_emails(query=message)
        elif "read" in msg:
            return await self._read_email(query=message)
        elif "free" in msg or "slot" in msg:
            return await self._free_slots()
        elif "event" in msg or "calendar" in msg:
            return await self._list_events()
        else:
            return await self._search_emails(query=message)

    # -- Typed tool methods (MCP introspects these signatures) --

    async def _search_emails(self, query: str, limit: int = 20) -> str:
        """Search emails by subject keyword."""
        return await self._exec("search_emails", {"query": query, "limit": limit})

    async def _read_email(self, query: str) -> str:
        """Read the full content of the most recent email matching a query."""
        return await self._exec("read_email", {"query": query})

    async def _list_events(self, days: int = 14) -> str:
        """List calendar events for the next N days."""
        return await self._exec("list_events", {"days": days})

    async def _free_slots(self, days: int = 14) -> str:
        """Calculate free meeting slots for the next N weekdays."""
        return await self._exec("free_slots", {"days": days})

    async def _create_draft(self, to: str, subject: str, body: str, cc: str = "") -> str:
        """Create an email draft."""
        return await self._exec("create_draft", {"to": to, "subject": subject, "body": body, "cc": cc})

    async def _draft_event(self, subject: str, start: str, end: str, location: str = "", body: str = "", attendees: str = "") -> str:
        """Create a calendar event draft (invitations NOT sent)."""
        return await self._exec("draft_event", {"subject": subject, "start": start, "end": end, "location": location, "body": body, "attendees": attendees})

    async def _send_event(self, subject: str, start: str, end: str, attendees: str, location: str = "", body: str = "") -> str:
        """Create a calendar event and send invitations immediately."""
        return await self._exec("send_event", {"subject": subject, "start": start, "end": end, "location": location, "body": body, "attendees": attendees})

    async def _draft_reply(self, query: str, body: str) -> str:
        """Create a reply draft to the most recent email matching a query."""
        return await self._exec("draft_reply", {"query": query, "body": body})

    async def _send_reply(self, query: str, body: str) -> str:
        """Reply to the most recent email matching a query and send immediately."""
        return await self._exec("send_reply", {"query": query, "body": body})

    async def _exec(self, tool_name: str, args: dict) -> str:
        """Submit tool call to the dedicated COM worker thread."""
        worker = self._ensure_worker()
        return await worker.execute(tool_name, args)


# --- App factory ---
_host = os.getenv("AGENT_HOST", "127.0.0.1")
_port = os.getenv("AGENT_PORT", "8001")
_service = EmailAgentService()
app = create_app(_service, base_url=f"http://{_host}:{_port}")
