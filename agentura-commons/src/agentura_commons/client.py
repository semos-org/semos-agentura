"""Headless MCP + A2A client with symmetric file middleware.

Reference client for testing and reuse by orchestrator, UI, and other consumers.
Composes MCPHub (SSE connections) + A2A client + FileRegistry + pre/post middleware.

Usage:
    client = AgenturaClient(
        agents={"document": "http://localhost:8002"},
    )
    async with client:
        # MCP tool call
        client.upload(Path("report.pdf"))
        result = await client.call_tool(
            "digest_document", {"source": "report.pdf"},
        )

        # A2A agent delegation
        r = await client.ask_agent(
            "document", "summarize this PDF",
            files=["report.pdf"],
        )
        print(r.text, r.task_id, r.status)
"""

from __future__ import annotations

import logging
import mimetypes
from dataclasses import dataclass, field
from pathlib import Path

import httpx

from .a2a_client import (
    A2AAgentInfo,
    close_all as _a2a_close_all,
    discover_agents as _a2a_discover,
    send_task as _a2a_send_task,
)
from .file_middleware import (
    FileEntry,
    FileRegistry,
    post_process_tool_result,
    pre_process_tool_call,
)
from .mcp_client import AgentConnection, MCPHub

logger = logging.getLogger(__name__)


@dataclass
class ClientToolResult:
    """Result of a tool call through the middleware."""

    text: str
    files: list[FileEntry] = field(default_factory=list)
    is_error: bool = False

    def format_for_llm(self) -> str:
        """Format result as text for an LLM prompt."""
        parts: list[str] = []
        if self.is_error:
            return f"[Error] {self.text}"
        if self.text:
            parts.append(self.text)
        if self.files:
            names = [f.filename for f in self.files]
            parts.append(f"[Produced: {', '.join(names)}]")
        return "\n".join(parts) if parts else "Done."


@dataclass
class ClientA2AResult:
    """Result of an A2A agent interaction through the middleware."""

    text: str
    agent_name: str = ""
    files: list[FileEntry] = field(default_factory=list)
    task_id: str | None = None
    context_id: str | None = None
    status: str = "completed"
    is_error: bool = False

    def format_for_llm(self) -> str:
        """Format result as text for an LLM prompt.

        Includes agent name, status indicators, file names, and
        continuation hints so the LLM can act on the result.
        """
        prefix = f"[{self.agent_name}] " if self.agent_name else ""
        parts: list[str] = []

        if self.status == "input_required":
            parts.append(f"{prefix}[Task {self.task_id} - needs input]")
            parts.append(f"Agent asks: {self.text}")
            parts.append("")
            agent_arg = f'"{self.agent_name}", ' if self.agent_name else ""
            parts.append(f'Continue with: ask_agent({agent_arg}message=<your answer>, task_id="{self.task_id}")')
            return "\n".join(parts)

        if self.status == "rejected":
            return f"{prefix}[Rejected] {self.text}"

        if self.status == "auth_required":
            return f"{prefix}[Auth required] {self.text}"

        if self.status == "failed":
            return f"{prefix}[Failed] {self.text}"

        # Completed
        if self.text:
            parts.append(f"{prefix}{self.text}")
        if self.files:
            names = [f.filename for f in self.files]
            parts.append(f"[Produced: {', '.join(names)}]")

        return "\n".join(parts) if parts else f"{prefix}Done."


class AgenturaClient:
    """Headless MCP client with symmetric file middleware.

    LLM only sees symbolic filenames. Middleware resolves file content
    on input and fetches/registers produced files on output.
    """

    def __init__(
        self,
        agents: dict[str, str],
        download_dir: Path | None = None,
    ) -> None:
        """
        Args:
            agents: Mapping of agent name to base URL.
                e.g. {"document": "http://localhost:8002"}
            download_dir: Directory for downloaded files.
                Defaults to a temp directory.
        """
        self._agent_urls = agents
        connections = [
            AgentConnection(
                name=name,
                url=f"{url}/mcp/sse",
                base_url=url.rstrip("/"),
            )
            for name, url in agents.items()
        ]
        self.hub = MCPHub(connections)
        self.registry = FileRegistry()
        self.download_dir = download_dir or Path(".")
        self._a2a_agents: dict[str, A2AAgentInfo] = {}

    async def connect(self) -> None:
        """Connect to all agents via MCP SSE and discover A2A."""
        await self.hub.connect_all()
        # Discover A2A agents (tolerates failures)
        self._a2a_agents = await _a2a_discover(self._agent_urls)

    async def close(self) -> None:
        """Disconnect from all agents."""
        await self.hub.disconnect_all()
        await _a2a_close_all(self._a2a_agents)

    async def __aenter__(self) -> AgenturaClient:
        await self.connect()
        return self

    async def __aexit__(self, *exc) -> None:
        await self.close()

    def upload(self, path: Path, name: str | None = None) -> str:
        """Register a local file in the file registry.

        Returns the symbolic filename the LLM should use.
        """
        filename = name or path.name
        blob = path.read_bytes()
        mime, _ = mimetypes.guess_type(str(path))
        self.registry.register(
            filename,
            blob,
            mime=mime or "application/octet-stream",
            source="upload",
        )
        logger.info("Uploaded %s (%d bytes)", filename, len(blob))
        return filename

    def upload_bytes(
        self,
        blob: bytes,
        filename: str,
        mime: str = "application/octet-stream",
    ) -> str:
        """Register raw bytes in the file registry."""
        self.registry.register(
            filename,
            blob,
            mime=mime,
            source="upload",
        )
        return filename

    @property
    def tools(self):
        """All tools from all connected agents."""
        return self.hub.all_tools()

    async def call_tool(
        self,
        name: str,
        args: dict,
    ) -> ClientToolResult:
        """Call a tool with file middleware.

        1. Pre-middleware: resolve symbolic filenames to content
        2. MCP call_tool
        3. Post-middleware: fetch produced files, register, strip URLs
        """
        # Find tool schema for file param detection
        tool_schema = self.hub.tool_schema(name)
        if tool_schema is None:
            return ClientToolResult(
                text=f"Unknown tool: {name}",
                is_error=True,
            )

        # Pre-middleware: resolve file references
        processed_args = pre_process_tool_call(
            name,
            args,
            tool_schema,
            self.registry,
        )

        # MCP call
        result = await self.hub.call_tool(name, processed_args)

        # Check for errors
        if result.isError:
            text = result.content[0].text if result.content and hasattr(result.content[0], "text") else "Tool error"
            return ClientToolResult(text=text, is_error=True)

        # Post-middleware: fetch files, strip URLs
        agent = self.hub.agent_for_tool(name)
        text, new_files = await post_process_tool_result(
            name,
            result,
            agent,
            self.registry,
        )

        return ClientToolResult(
            text=text,
            files=new_files,
        )

    # A2A agent delegation

    @property
    def a2a_agents(self) -> dict[str, A2AAgentInfo]:
        """Discovered A2A agents."""
        return self._a2a_agents

    async def ask_agent(
        self,
        name: str,
        message: str,
        files: list[str] | None = None,
        data: dict | None = None,
        task_id: str | None = None,
    ) -> ClientA2AResult:
        """Delegate a task to an agent via A2A.

        The LLM calls this with symbolic filenames. Middleware resolves
        them to inline base64 content. Produced files are fetched and
        registered back in the registry.

        Args:
            name: Agent name (must match key in agents dict).
            message: Natural language request.
            files: Symbolic filenames from the registry.
            data: Optional structured data dict.
            task_id: Continue an existing task (multi-turn).

        Returns:
            ClientA2AResult with LLM-safe text (no URLs),
            registered files, task_id, and status.
        """
        agent = self._a2a_agents.get(name)
        if not agent:
            return ClientA2AResult(
                text=f"Agent '{name}' not found via A2A.",
                agent_name=name,
                is_error=True,
            )

        # Resolve symbolic filenames to FileAttachment dicts
        file_dicts = []
        for fname in files or []:
            entry = self.registry.get(fname)
            if entry and entry.blob:
                import base64 as _b64

                b64 = _b64.b64encode(entry.blob).decode()
                file_dicts.append(
                    {
                        "name": fname,
                        "content": f"data:{entry.mime};base64,{b64}",
                    }
                )
            else:
                logger.warning(
                    "File '%s' not in registry, skipping",
                    fname,
                )

        # Send via A2A
        result = await _a2a_send_task(
            agent,
            message,
            files=file_dicts or None,
            data=data,
            task_id=task_id,
        )

        # Fetch produced files and register them
        registered_files = []
        for fi in result.files:
            if not fi.url:
                continue
            try:
                async with httpx.AsyncClient(timeout=30) as hc:
                    resp = await hc.get(fi.url)
                    resp.raise_for_status()
                fname = fi.filename or fi.url.rsplit("/", 1)[-1]
                self.registry.register(
                    fname,
                    resp.content,
                    mime=fi.mime_type,
                    source=f"a2a:{name}",
                )
                registered_files.append(
                    self.registry.get(fname),
                )
                logger.info(
                    "A2A file registered: %s (%d bytes)",
                    fname,
                    len(resp.content),
                )
            except Exception:
                logger.warning(
                    "Failed to fetch A2A file %s",
                    fi.url,
                )

        # Strip URLs from text (LLM should only see filenames)
        text = result.text
        for fi in result.files:
            if fi.url and fi.url in text:
                text = text.replace(fi.url, fi.filename or "")

        return ClientA2AResult(
            text=text,
            agent_name=name,
            files=[f for f in registered_files if f],
            task_id=result.task_id,
            context_id=result.context_id,
            status=result.status,
            is_error=result.status == "failed",
        )
