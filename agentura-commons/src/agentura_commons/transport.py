"""Unified FastAPI app: MCP (SSE) + A2A + health + file serving."""

from __future__ import annotations

import logging
import time
from pathlib import Path
from typing import TYPE_CHECKING

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from .a2a_server import create_a2a_handler, create_agent_card
from .mcp_server import create_mcp_server

if TYPE_CHECKING:
    from .base import BaseAgentService

logger = logging.getLogger(__name__)

# Output files older than this are deleted on startup.
MAX_FILE_AGE_SECONDS = 24 * 60 * 60


def _cleanup_old_files(directory: Path, max_age: int = MAX_FILE_AGE_SECONDS) -> None:
    """Delete files older than max_age seconds."""
    if not directory.exists():
        return
    now = time.time()
    for f in directory.iterdir():
        if f.is_file() and (now - f.stat().st_mtime) > max_age:
            logger.info("Cleaning up old output file: %s", f.name)
            f.unlink(missing_ok=True)


def _output_dir_for(service: BaseAgentService) -> Path:
    """Return a fixed output directory under the workspace root."""
    slug = service.agent_name.lower().replace(" ", "-")
    # Walk up from this file to find the workspace root (has uv.lock)
    here = Path(__file__).resolve().parent
    for parent in (here, *here.parents):
        if (parent / "uv.lock").exists():
            d = parent / "output" / slug
            d.mkdir(parents=True, exist_ok=True)
            return d
    # Fallback: cwd
    d = Path("output") / slug
    d.mkdir(parents=True, exist_ok=True)
    return d


def create_app(
    service: BaseAgentService,
    *,
    base_url: str | None = None,
) -> FastAPI:
    """Build a FastAPI app with MCP SSE + A2A + /health + /files.

    Routes:
        GET  /health                      -> health check
        GET  /mcp/sse                     -> MCP SSE stream
        POST /mcp/messages/               -> MCP messages
        GET  /.well-known/agent-card.json -> A2A Agent Card
        POST /a2a                         -> A2A JSON-RPC (when implemented)
        GET  /files/<filename>            -> download output files
    """
    url = base_url or "http://127.0.0.1:8000"

    # Output directory for files produced by tools
    output_dir = _output_dir_for(service)
    _cleanup_old_files(output_dir)
    service.output_dir = output_dir
    service.base_url = url

    app = FastAPI(
        title=service.agent_name,
        description=service.agent_description,
        version=service.agent_version,
    )

    @app.get("/health")
    async def health():
        return {"status": "ok", "agent": service.agent_name}

    # Static file serving for tool outputs
    app.mount("/files", StaticFiles(directory=str(output_dir)), name="files")

    # MCP SSE (mount_path="" - sub-app is already at /mcp)
    mcp_server = create_mcp_server(service)
    mcp_sse = mcp_server.sse_app(mount_path="")
    app.mount("/mcp", mcp_sse)

    # A2A: REST + JSON-RPC (dual binding)
    try:
        from a2a.server.apps.jsonrpc.fastapi_app import (
            A2AFastAPIApplication,
        )
        from a2a.server.apps.rest.fastapi_app import (
            A2ARESTFastAPIApplication,
        )
        from fastapi.responses import JSONResponse
        from google.protobuf.json_format import MessageToDict

        agent_card = create_agent_card(service, base_url=url)
        a2a_handler = create_a2a_handler(service)

        # REST (HTTP+JSON) at /a2a/*
        a2a_rest = A2ARESTFastAPIApplication(
            agent_card=agent_card,
            http_handler=a2a_handler,
        )
        a2a_rest_app = a2a_rest.build(rpc_url="/a2a")
        for route in a2a_rest_app.routes:
            app.routes.append(route)

        # JSON-RPC at /a2a/rpc
        a2a_rpc = A2AFastAPIApplication(
            agent_card=agent_card,
            http_handler=a2a_handler,
        )
        a2a_rpc.add_routes_to_app(app, rpc_url="/a2a/rpc")

        # Agent card (shared, served at well-known path)
        @app.get("/.well-known/agent-card.json")
        async def get_agent_card():
            return JSONResponse(
                content=MessageToDict(agent_card),
            )

        logger.info(
            "A2A routes: REST at /a2a, JSON-RPC at /a2a/rpc",
        )
    except ImportError:
        logger.warning(
            "a2a-sdk not available, A2A routes not mounted",
        )

    return app
