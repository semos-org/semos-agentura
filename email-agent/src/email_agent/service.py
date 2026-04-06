"""MCP + A2A service wrapper for email-agent.

Usage:
    uvicorn email_agent.service:app --port 8001
"""

from __future__ import annotations

import asyncio
import base64
import logging
import os
import queue
import threading
import uuid
from pathlib import Path
from typing import Any

from agentura_commons import BaseAgentService, FileAttachment, SkillDef, ToolDef, create_app

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

    def _resolve_file(self, source: str, default_ext: str = ".bin", filename: str = "") -> Path:
        """Resolve source as a file path, base64, or data URI.

        If filename is provided, the temp file preserves that name
        (important for email attachments where the recipient sees it).
        """
        p = Path(source)
        if p.exists():
            return p
        raw = source
        if raw.startswith("data:"):
            _, encoded = raw.split(",", 1)
            raw = encoded
        try:
            data = base64.b64decode(raw, validate=True)
            if len(data) > 4:
                if filename:
                    # Preserve original filename in a UUID-prefixed subdir
                    subdir = self.output_dir / f"_att_{uuid.uuid4().hex[:8]}"
                    subdir.mkdir(exist_ok=True)
                    tmp = subdir / filename
                else:
                    tmp = self.output_dir / f"_upload_{uuid.uuid4().hex[:8]}{default_ext}"
                tmp.write_bytes(data)
                return tmp
        except Exception:
            pass

        # URL fallback: fetch the file content
        if source.startswith(("http://", "https://")):
            import httpx as _httpx

            resp = _httpx.get(source, timeout=30)
            resp.raise_for_status()
            ext = Path(source.rsplit("/", 1)[-1]).suffix or default_ext
            fn = filename or f"_fetch_{uuid.uuid4().hex[:8]}{ext}"
            tmp = self.output_dir / fn
            tmp.write_bytes(resp.content)
            return tmp

        return p

    def get_tools(self) -> list[ToolDef]:
        _fh = "Accepts absolute file paths or base64-encoded content."
        return [
            ToolDef(
                name="search_emails",
                description="Search emails with composable filters: subject, sender, recipient, date range, unread, attachments. All optional, AND-combined.",
                fn=self._search_emails,
                read_only=True,
                idempotent=True,
            ),
            ToolDef(
                name="read_email",
                description="Read the full content of the most recent email matching filters (subject, sender, recipient).",
                fn=self._read_email,
                read_only=True,
                idempotent=True,
            ),
            ToolDef(
                name="list_events",
                description="List calendar events for the next N days.",
                fn=self._list_events,
                read_only=True,
                idempotent=True,
            ),
            ToolDef(
                name="free_slots",
                description="Calculate free meeting slots for the next N weekdays.",
                fn=self._free_slots,
                read_only=True,
                idempotent=True,
            ),
            ToolDef(
                name="create_draft",
                description=f"Create an email draft with optional attachments. {_fh}",
                fn=self._create_draft,
                file_params=["attachments"],
            ),
            ToolDef(
                name="draft_event",
                description="Create a calendar event draft (invitations NOT sent).",
                fn=self._draft_event,
            ),
            ToolDef(
                name="send_event",
                description="Create a calendar event and send invitations immediately.",
                fn=self._send_event,
                destructive=True,
            ),
            ToolDef(
                name="draft_reply",
                description="Create a reply draft to the most recent email matching a query.",
                fn=self._draft_reply,
            ),
            ToolDef(
                name="send_reply",
                description="Reply to the most recent email matching a query and send immediately.",
                fn=self._send_reply,
                destructive=True,
            ),
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

        # Post-process: convert saved_path to download_url
        if include_attachments and self.output_dir:
            result = _json.loads(raw)
            for att in result.get("attachments", []):
                saved = att.pop("saved_path", None)
                if saved:
                    saved_p = Path(saved)
                    safe_name = f"{_uuid.uuid4().hex[:8]}_{saved_p.name}"
                    dest = self.output_dir / safe_name
                    shutil.move(str(saved_p), str(dest))
                    att["download_url"] = self.file_url(safe_name)
                    att["size_bytes"] = dest.stat().st_size
            raw = _json.dumps(result, ensure_ascii=False)

        return raw

    async def _list_events(self, days: int = 14) -> str:
        """List calendar events for the next N days."""
        return await self._exec("list_events", {"days": days})

    async def _free_slots(self, days: int = 14) -> str:
        """Calculate free meeting slots for the next N weekdays."""
        return await self._exec("free_slots", {"days": days})

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
            resolved = self._resolve_file(content, default_ext=ext, filename=name)
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
        """Submit tool call to the dedicated COM worker thread."""
        worker = self._ensure_worker()
        return await worker.execute(tool_name, args)


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
