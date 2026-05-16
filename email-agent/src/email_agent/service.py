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
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

# Load .env from agent dir, then workspace root
_agent_dir = Path(__file__).resolve().parent.parent.parent
load_dotenv(_agent_dir / ".env")
load_dotenv(_agent_dir.parent / ".env")

from agentura_commons import BaseAgentService, FileAttachment, SkillDef, create_app

from .backend import create_backend
from .config import Settings
from .tools import ToolExecutor

logger = logging.getLogger(__name__)


class _COMWorker:
    """Dedicated single thread for Outlook COM operations.

    COM objects are apartment-threaded on Windows - they can only be used
    from the thread that created them. This worker creates the backend once
    and processes all tool calls sequentially on that thread.

    Only used when the backend is COM. IMAP backends use asyncio.to_thread.
    """

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._queue: queue.Queue[tuple[str, dict, asyncio.Future]] = queue.Queue()
        self._thread = threading.Thread(target=self._run, daemon=True, name="com-worker")
        self._thread.start()

    def _run(self) -> None:
        """Worker loop - runs on the dedicated COM thread."""
        try:
            backend = create_backend(self._settings)
            backend.connect()
        except Exception as e:
            logger.error("COM backend init failed: %s", e)
            # Drain queue with errors
            while True:
                tool_name, args, future = self._queue.get()
                loop = future.get_loop()
                loop.call_soon_threadsafe(
                    self._safe_set_exception,
                    future,
                    RuntimeError(f"Backend not available: {e}"),
                )
            return
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


class _AsyncExecutor:
    """Non-COM tool executor using asyncio.to_thread.

    For IMAP and other thread-safe backends. No dedicated thread needed.
    """

    def __init__(self, settings: Settings) -> None:
        self._executor: ToolExecutor | None = None
        self._settings = settings

    def _ensure(self) -> ToolExecutor:
        if self._executor is None:
            backend = create_backend(self._settings)
            backend.connect()
            self._executor = ToolExecutor(backend)
            logger.info("Async executor ready (backend: %s)", self._settings.detected_backend)
        return self._executor

    async def execute(self, tool_name: str, args: dict) -> str:
        executor = self._ensure()
        return await asyncio.to_thread(executor.execute, tool_name, args)


class EmailAgentService(BaseAgentService):
    """Exposes email-agent's tools via MCP and skills via A2A."""

    def __init__(self) -> None:
        self._settings = Settings()
        self._executor_impl: _COMWorker | _AsyncExecutor | None = None
        self._backend = None

    def _ensure_executor(self) -> _COMWorker | _AsyncExecutor:
        """Lazily create the right executor based on backend type."""
        if self._executor_impl is None:
            if self._settings.detected_backend == "com":
                self._executor_impl = _COMWorker(self._settings)
                logger.info("Using COM worker thread")
            else:
                self._executor_impl = _AsyncExecutor(self._settings)
                logger.info("Using async executor (backend: %s)", self._settings.detected_backend)
        return self._executor_impl

    @property
    def agent_name(self) -> str:
        return "Email Agent"

    @property
    def agent_description(self) -> str:
        return "Email and calendar operations - search, read, send, draft, reply, and manage events."

    @property
    def agent_version(self) -> str:
        return "0.2.0"

    @property
    def agent_system_prompt(self) -> str:
        return (
            super().agent_system_prompt + "\n\n"
            "You are an email and calendar assistant. "
            "Use the specialized tools - do NOT try to compute results manually. "
            "For availability or free time queries, ALWAYS use the free_slots tool "
            "(not list_events). Use list_events only when the user needs event details "
            "(subjects, attendees, locations). "
            "When a date range is specified, pass start and end as YYYY-MM-DD strings. "
            "NEVER guess the current year, month, day, time, or day-of-week mappings. "
            "Always use the current date/time provided above."
        )

    # _resolve_file and _resolve_file_attachment inherited from BaseAgentService

    def get_tools(self) -> list:
        from .tools import get_email_tools

        return get_email_tools(self)

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

    async def _search_emails(
        self,
        query: str = "",
        limit: int = 20,
        from_addr: str = "",
        to_addr: str = "",
        since: str = "",
        before: str = "",
        unread_only: bool = False,
        has_attachments: bool | None = None,
    ) -> str:
        """Search emails with composable filters. All filters are AND-combined.

        Args:
            query: Subject keyword (partial match). Optional.
            limit: Max results to return.
            from_addr: Sender email address (partial match).
            to_addr: Recipient email address (partial match).
            since: Only emails on or after this date (YYYY-MM-DD).
            before: Only emails before this date (YYYY-MM-DD).
            unread_only: Only return unread emails.
            has_attachments: true = only with attachments, false = only without.
        """
        return await self._exec(
            "search_emails",
            {
                "query": query,
                "limit": limit,
                "from_addr": from_addr,
                "to_addr": to_addr,
                "since": since,
                "before": before,
                "unread_only": unread_only,
                "has_attachments": has_attachments,
            },
        )

    async def _read_email(
        self,
        entry_id: str = "",
        query: str = "",
        from_addr: str = "",
        to_addr: str = "",
        include_attachments: bool = False,
    ) -> str:
        """Read the full content of an email.

        Use entry_id for exact lookup (from search_emails results).
        Or use filters to find the most recent match.

        Args:
            entry_id: Exact email ID from search_emails results. Preferred.
            query: Subject keyword (partial match). Used if no entry_id.
            from_addr: Sender email address (partial match).
            to_addr: Recipient email address (partial match).
            include_attachments: If true, save attachments and return download URLs.
        """
        import json as _json
        import shutil
        import uuid as _uuid

        # Create a temp subdir for attachments if requested
        att_dir = None
        if include_attachments and self.output_dir:
            att_dir = str(self.output_dir / f"_att_{_uuid.uuid4().hex[:8]}")

        raw = await self._exec(
            "read_email",
            {
                "entry_id": entry_id,
                "query": query,
                "from_addr": from_addr,
                "to_addr": to_addr,
                "include_attachments": include_attachments,
                "_attachment_dir": att_dir,
            },
        )

        # Post-process: move saved attachments to output_dir.
        # Return ToolResult with data + files so the normalizer
        # creates proper ResourceLink blocks (MCP spec).
        from agentura_commons.base import NamedFile, ToolResult

        result = _json.loads(raw)
        files: list[NamedFile] = []
        if include_attachments and self.output_dir:
            for att in result.get("attachments", []):
                saved = att.pop("saved_path", None)
                if saved:
                    saved_p = Path(saved)
                    safe_name = f"{_uuid.uuid4().hex[:8]}_{saved_p.name}"
                    dest = self.output_dir / safe_name
                    shutil.move(str(saved_p), str(dest))
                    att["file"] = saved_p.name
                    att["size_bytes"] = dest.stat().st_size
                    files.append(NamedFile(path=dest, name=saved_p.name))

        return ToolResult(data=result, files=files)

    async def _list_events(self, start: str = "", end: str = "", days: int = 14) -> str:
        """List calendar events in a date range.

        Args:
            start: Start date (YYYY-MM-DD). Defaults to today.
            end: End date (YYYY-MM-DD). If set, days is ignored.
            days: Number of days from start. Only used when end is not set.
        """
        return await self._exec("list_events", {"start": start, "end": end, "days": days})

    async def _free_slots(self, start: str = "", end: str = "", days: int = 14) -> str:
        """Calculate free meeting slots during business hours.

        Args:
            start: Start date (YYYY-MM-DD). Defaults to today.
            end: End date (YYYY-MM-DD). If set, days is ignored.
            days: Number of days from start. Only used when end is not set.
        """
        return await self._exec("free_slots", {"start": start, "end": end, "days": days})

    async def _create_draft(
        self,
        to: str,
        subject: str,
        body: str,
        cc: str = "",
        attachments: list[FileAttachment] | None = None,
    ) -> str:
        """Create an email draft with optional attachments.

        Args:
            to: Recipient email address(es), semicolon-separated.
            subject: Email subject line.
            body: Email body text.
            cc: CC recipients, semicolon-separated.
            attachments: Array of file objects with 'name' and 'content' fields.
                Example: [{"name": "report.docx", "content": "/path/to/file.docx"}]
                The content field accepts a file path, base64, or data URI.
        """
        att_paths = []
        for item in attachments or []:
            name = item.get("name", "")
            content = item.get("content", name)  # fallback: name is the path
            ext = Path(name).suffix if name else ".bin"
            resolved = self.resolve_file(content, default_ext=ext, filename=name)
            logger.info("Resolved attachment: %s -> %s", name, resolved)
            att_paths.append(str(resolved))

        args = {"to": to, "subject": subject, "body": body, "cc": cc}
        if att_paths:
            args["attachments"] = att_paths
        return await self._exec("create_draft", args)

    async def _draft_event(
        self, subject: str, start: str, end: str, location: str = "", body: str = "", attendees: str = ""
    ) -> str:
        """Create a calendar event draft (invitations NOT sent)."""
        return await self._exec(
            "draft_event",
            {
                "subject": subject,
                "start": start,
                "end": end,
                "location": location,
                "body": body,
                "attendees": attendees,
            },
        )

    async def _send_event(
        self, subject: str, start: str, end: str, attendees: str, location: str = "", body: str = ""
    ) -> str:
        """Create a calendar event and send invitations immediately."""
        return await self._exec(
            "send_event",
            {
                "subject": subject,
                "start": start,
                "end": end,
                "location": location,
                "body": body,
                "attendees": attendees,
            },
        )

    async def _draft_reply(self, query: str, body: str) -> str:
        """Create a reply draft to the most recent email matching a query."""
        return await self._exec("draft_reply", {"query": query, "body": body})

    async def _send_reply(self, query: str, body: str) -> str:
        """Reply to the most recent email matching a query and send immediately."""
        return await self._exec("send_reply", {"query": query, "body": body})

    async def _exec(self, tool_name: str, args: dict) -> str:
        """Execute tool via the appropriate backend executor."""
        executor = self._ensure_executor()
        return await executor.execute(tool_name, args)


# --- App factory ---
_service = EmailAgentService()


def create_service_app(
    host: str | None = None,
    port: str | int | None = None,
):
    """Create the FastAPI app. Called lazily by uvicorn."""
    h = host or os.getenv("AGENT_HOST", "127.0.0.1")
    p = port or os.getenv("AGENT_PORT", "8001")
    return create_app(_service, base_url=f"http://{h}:{p}")


app = create_service_app()
