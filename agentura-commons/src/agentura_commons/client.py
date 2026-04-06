"""Headless MCP client with symmetric file middleware.

Reference client for testing and reuse by orchestrator, UI, and other consumers.
Composes MCPHub (SSE connections) + FileRegistry (file tracking) + pre/post middleware.

Usage:
    client = AgenturaClient(
        agents={"document": "http://localhost:8002"},
    )
    async with client:
        client.upload(Path("report.pdf"))
        result = await client.call_tool(
            "digest_document", {"source": "report.pdf"},
        )
        print(result.text)       # LLM-safe text (no URLs)
        print(result.files)      # [FileEntry(...)]
"""

from __future__ import annotations

import logging
import mimetypes
from dataclasses import dataclass, field
from pathlib import Path

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

    async def connect(self) -> None:
        """Connect to all agents via MCP SSE."""
        await self.hub.connect_all()

    async def close(self) -> None:
        """Disconnect from all agents."""
        await self.hub.disconnect_all()

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
