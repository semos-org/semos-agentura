"""MCP + A2A service wrapper for email-agent.

Usage:
    uvicorn semos.agentura.email.service:app --port 8001
"""

from __future__ import annotations

import asyncio
import logging
import os
import queue
import threading
from collections.abc import Callable
from pathlib import Path
from typing import Any, TypeVar

from dotenv import load_dotenv

# Load .env from agent dir, then workspace root
_agent_dir = Path(__file__).resolve().parent.parent.parent
load_dotenv(_agent_dir / ".env")
load_dotenv(_agent_dir.parent / ".env")

from semos.agentura.core import BaseAgentService, SkillDef, create_app

from .backend import EmailBackend, create_backend
from .config import Settings

logger = logging.getLogger(__name__)

T = TypeVar("T")


class _COMWorker:
    """Dedicated single thread for Outlook COM operations.

    COM objects are apartment-threaded on Windows - they can only be used
    from the thread that created them. This worker creates the backend once
    and processes all calls sequentially on that thread.
    """

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._queue: queue.Queue = queue.Queue()
        self._thread = threading.Thread(target=self._run, daemon=True, name="com-worker")
        self._thread.start()

    def _run(self) -> None:
        try:
            backend = create_backend(self._settings)
            backend.connect()
        except Exception as e:
            logger.error("COM backend init failed: %s", e)
            while True:
                fn, future = self._queue.get()
                loop = future.get_loop()
                loop.call_soon_threadsafe(
                    _safe_set_exception,
                    future,
                    RuntimeError(f"Backend not available: {e}"),
                )
            return
        logger.info("COM worker thread started")

        while True:
            fn, future = self._queue.get()
            try:
                result = fn(backend)
                loop = future.get_loop()
                loop.call_soon_threadsafe(_safe_set_result, future, result)
            except Exception as e:
                loop = future.get_loop()
                loop.call_soon_threadsafe(_safe_set_exception, future, e)

    async def run(self, fn: Callable[[EmailBackend], T]) -> T:
        """Submit fn(backend) to the COM thread and await result."""
        future: asyncio.Future[T] = asyncio.get_running_loop().create_future()
        self._queue.put((fn, future))
        return await future


def _safe_set_result(future: asyncio.Future, result: Any) -> None:
    if not future.done():
        future.set_result(result)


def _safe_set_exception(future: asyncio.Future, exc: Exception) -> None:
    if not future.done():
        future.set_exception(exc)


class EmailAgentService(BaseAgentService):
    """Exposes email-agent's tools via MCP and skills via A2A."""

    def __init__(self) -> None:
        self._settings = Settings()
        self._com_worker: _COMWorker | None = None
        self._backend_instance: EmailBackend | None = None

    async def run_sync(self, fn: Callable[[EmailBackend], T]) -> T:
        """Run fn(backend) on the appropriate thread.

        COM: dedicated worker thread (apartment-threaded).
        IMAP/Graph: asyncio.to_thread (thread-safe).
        Pre-injected mock: used directly (tests).
        """
        # If backend already injected (mock or previous init), use it
        if self._backend_instance is not None:
            return await asyncio.to_thread(fn, self._backend_instance)
        if self._settings.detected_backend == "com":
            if self._com_worker is None:
                self._com_worker = _COMWorker(self._settings)
            return await self._com_worker.run(fn)
        self._backend_instance = create_backend(self._settings)
        self._backend_instance.connect()
        logger.info(
            "Backend connected: %s",
            self._settings.detected_backend,
        )
        return await asyncio.to_thread(fn, self._backend_instance)

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
            super().agent_system_prompt + "\n\n" + "You are an email and calendar assistant. "
            "Use the specialized tools - do NOT try to "
            "compute results manually. "
            "For availability or free time queries, ALWAYS use "
            "the free_slots tool (not list_events). "
            "Use list_events only when the user needs event "
            "details (subjects, attendees, locations). "
            "NEVER guess the current year, month, day, time, "
            "or day-of-week mappings. "
            "Always use the current date/time provided above."
        )

    def get_tools(self) -> list:
        from .tools import get_email_tools

        return get_email_tools(self)

    def get_skills(self) -> list[SkillDef]:
        return [
            SkillDef(
                id="email-operations",
                name="Email Operations",
                description=("Search, read, send, and draft emails and calendar events."),
                tags=["email", "calendar", "outlook"],
            ),
        ]

    async def execute_skill(
        self,
        skill_id: str,
        message: str,
        *,
        task_id: str | None = None,
    ) -> str:
        msg = message.lower()
        tools = self.get_tools()
        if "search" in msg:
            t = next(t for t in tools if t.name == "search_emails")
            return await t._arun(query=message)
        elif "free" in msg or "slot" in msg:
            t = next(t for t in tools if t.name == "free_slots")
            return await t._arun()
        elif "event" in msg or "calendar" in msg:
            t = next(t for t in tools if t.name == "list_events")
            return await t._arun()
        else:
            t = next(t for t in tools if t.name == "search_emails")
            return await t._arun(query=message)


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
