"""Entry point for Agentura UI.

Usage:
    uv run python -m agentura_ui
"""

from __future__ import annotations

import asyncio
import logging
import os
from pathlib import Path

import panel as pn
from dotenv import load_dotenv
from panelini import Panelini
from panelini.panels.ai.frontend import AVAILABLE_TOOLS
from panelini.panels.ai.frontend import AiChat as Frontend
from panelini.panels.ai.utils.ai_interface import (
    PROVIDER_CLASS_REGISTRY,
)

from filesystem_agent.panel_tree import VFSTreeBrowser
from filesystem_agent.vfs import VirtualFileSystem

from .a2a_client import discover_agents
from .a2a_tools import create_a2a_delegates
from .file_registry import VFSFileRegistry, human_size
from .mcp_hub import AgentConnection, MCPHub
from .mcp_tools import (
    drain_produced_files,
    set_file_notify_callback,
    set_status_callback,
    set_vfs_changed_callback,
)
from .renderers import (
    render_file_notification,
    resolve_file_references,
)

logger = logging.getLogger(__name__)

_PKG_DIR = Path(__file__).resolve().parent
_UI_DIR = _PKG_DIR.parent.parent  # agentura-ui/
_CONFIG_YML = _UI_DIR / "config.yml"

_SYSTEM_MESSAGE = """\
You are a helpful assistant that orchestrates tasks across \
multiple agents via tools.

PLANNING:
- For multi-step requests, plan the steps first, then execute \
them one by one using the available tools.
- Chain results: tool outputs produce files. Use the output \
filename from one step as input to the next.
- You can combine tools from different agents in one workflow.

FILES:
- The system resolves filenames automatically. Never ask for \
paths or base64 - just use the filename.
- Uploaded files appear as "I have uploaded: report.pdf (240 KB)".
- Tool outputs appear as "File created: output.pdf (1.2 MB)".
- Pass exact filenames between tool calls to chain them.

TOOL SELECTION:
- Use specific tools when you know which operation is needed.
- Use ask_* delegate tools for open-ended requests where the \
agent should decide how to proceed.
"""


def _register_litellm_provider() -> None:
    """Register 'litellm' client type in panelini's provider registry."""
    if "litellm" in PROVIDER_CLASS_REGISTRY:
        return

    def _create(provider, model_name, temperature, max_tokens):
        from langchain_community.chat_models import ChatLiteLLM

        api_key = provider.env_vars.get("api_key", "")
        api_base = provider.env_vars.get("api_base", "")
        if api_key:
            os.environ.setdefault("ANTHROPIC_API_KEY", api_key)
        if api_base:
            base = api_base.rstrip("/")
            os.environ.setdefault("ANTHROPIC_BASE_URL", base)

        return ChatLiteLLM(
            model=model_name,
            temperature=temperature,
            max_tokens=max_tokens,
        )

    PROVIDER_CLASS_REGISTRY["litellm"] = _create


def _build_agents() -> list[AgentConnection]:
    return [
        AgentConnection(
            name="email-agent",
            url=os.environ.get(
                "EMAIL_AGENT_URL",
                "http://localhost:8001/mcp/sse",
            ),
            base_url=os.environ.get(
                "EMAIL_AGENT_BASE",
                "http://localhost:8001",
            ),
        ),
        AgentConnection(
            name="document-agent",
            url=os.environ.get(
                "DOCUMENT_AGENT_URL",
                "http://localhost:8002/mcp/sse",
            ),
            base_url=os.environ.get(
                "DOCUMENT_AGENT_BASE",
                "http://localhost:8002",
            ),
        ),
        AgentConnection(
            name="filesystem-agent",
            url=os.environ.get(
                "FILESYSTEM_AGENT_URL",
                "http://localhost:8003/mcp/sse",
            ),
            base_url=os.environ.get(
                "FILESYSTEM_AGENT_BASE",
                "http://localhost:8003",
            ),
        ),
    ]


def _wrap_chat_callback(original_callback, registry, pending_uploads,
                        _unused=None):
    """Wrap Frontend chat callback to prepend file context,
    resolve file references, and register tool outputs."""

    async def wrapped(contents, user, instance):
        # Guard: never send empty content to the LLM
        if not isinstance(contents, str) or not contents.strip():
            yield "Please enter a message."
            return

        # Prepend uploaded-file context so the LLM knows
        # which files are available for tool calls.
        if pending_uploads:
            file_list = ", ".join(pending_uploads)
            contents = f"[Uploaded files available: {file_list}]\n\n" + contents
            pending_uploads.clear()

        # Delegate to the original Frontend callback.
        last_chunk = None
        result = original_callback(contents, user, instance)
        if hasattr(result, "__aiter__"):
            async for chunk in result:
                last_chunk = chunk
                yield chunk
        elif asyncio.iscoroutine(result):
            last_chunk = await result
            if last_chunk:
                yield last_chunk
        elif result is not None:
            last_chunk = result
            yield result

        # Resolve markdown file refs (![](file.png)) to
        # inline data URIs so images render in chat.
        if last_chunk and isinstance(last_chunk, str):
            resolved = resolve_file_references(
                last_chunk,
                registry,
            )
            if resolved != last_chunk:
                yield resolved

        # Files are notified in real-time via _on_file_produced
        # callback. Drain the list here to prevent accumulation.
        drain_produced_files()

    return wrapped


def _start_filesystem_agent_inprocess(
    vfs: VirtualFileSystem, port: int,
) -> None:
    """Start the filesystem-agent as a uvicorn thread sharing our VFS.

    The agent exposes MCP SSE + A2A on the given port so the UI's
    MCP tool wrappers connect to it like any other agent. But the
    VFS instance is shared - files written by tools or by the UI's
    file registry are visible to both.
    """
    import socket
    import threading

    import uvicorn

    from filesystem_agent.service import FilesystemAgentService

    # Check if port is already in use (external agent running)
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        if s.connect_ex(("127.0.0.1", port)) == 0:
            logger.info(
                "Filesystem-agent port %d in use, "
                "skipping in-process start",
                port,
            )
            return

    from agentura_commons import create_app as create_agent_app

    service = FilesystemAgentService(vfs=vfs)
    agent_app = create_agent_app(
        service, base_url=f"http://127.0.0.1:{port}",
    )

    config = uvicorn.Config(
        agent_app,
        host="127.0.0.1",
        port=port,
        log_level="warning",
    )
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()

    # Wait for startup
    import time

    for _ in range(50):
        time.sleep(0.1)
        if server.started:
            break

    logger.info(
        "Filesystem-agent started in-process on port %d "
        "(shared VFS with %d roots)",
        port, len(vfs.roots),
    )


# Module-level state (single-user app)
_hub: MCPHub | None = None
_vfs = VirtualFileSystem()
_vfs.add_root_from_protocol("session", "memory", base_path="/")
_registry = VFSFileRegistry(_vfs, root="session")
_fs_agent_port: int | None = None  # set in main()


def create_app() -> Panelini:
    """Per-session app factory called by pn.serve."""
    frontend = Frontend(
        system_message=_SYSTEM_MESSAGE,
        config_path=_CONFIG_YML,
    )

    # Increase max tool iterations (panelini default is 10).
    # A2A delegates with multi-step workflows need more.
    # Patch max tool iterations (panelini default is 10).
    import types

    from langchain_core.messages import AIMessage

    async def _handle_with_more_iterations(self, user_message):
        if not self.ai_interface:
            return "Error: AI interface not initialized"
        max_iterations = 50
        iteration = 0
        while iteration < max_iterations:
            if iteration == 0:
                response_data = await self.ai_interface.get_response_with_tools(user_message)
            else:
                response = await self.ai_interface.model.ainvoke(
                    self.ai_interface.conversation_history,
                )
                response_text = response.content if isinstance(response.content, str) else str(response.content)
                tool_calls = getattr(response, "tool_calls", [])
                self.ai_interface.conversation_history.append(
                    AIMessage(
                        content=response_text,
                        tool_calls=tool_calls,
                    ),
                )
                response_data = {
                    "text": response_text,
                    "tool_calls": tool_calls,
                }
            if not response_data.get("tool_calls"):
                return str(response_data.get("text", ""))
            tool_results = await self._execute_tool_calls(
                response_data["tool_calls"],
            )
            self.ai_interface.conversation_history.extend(
                tool_results,
            )
            iteration += 1
        return "Maximum tool execution iterations reached."

    frontend.backend._handle_message_with_tools = types.MethodType(
        _handle_with_more_iterations,
        frontend.backend,
    )

    # Status updates: send as italicized System messages in chat.
    # These appear immediately during tool execution, giving
    # the user feedback on what's happening.
    _last_status_msg = [None]  # mutable ref for closure

    def _on_status(text):
        if text:
            # Remove previous status message (replace, not accumulate)
            if _last_status_msg[0] is not None:
                try:
                    objs = frontend.chat_interface.objects
                    if _last_status_msg[0] in objs:
                        objs.remove(_last_status_msg[0])
                except Exception:
                    pass
            msg = frontend.chat_interface.send(
                f"*{text}*",
                user="System",
                respond=False,
            )
            _last_status_msg[0] = msg
        else:
            # Clear status
            if _last_status_msg[0] is not None:
                try:
                    objs = frontend.chat_interface.objects
                    if _last_status_msg[0] in objs:
                        objs.remove(_last_status_msg[0])
                except Exception:
                    pass
                _last_status_msg[0] = None

    set_status_callback(_on_status)

    # Enable only delegate tools (ask_*) by default.
    for name, info in frontend.tool_checkboxes.items():
        info["checkbox"].value = name.startswith("ask_")
    frontend.backend.update_tools(frontend._get_selected_tools())
    logger.info(
        "Default tools: %s",
        [n for n, i in frontend.tool_checkboxes.items() if i["checkbox"].value],
    )
    # Clear the "Tools updated" spam, keep only the welcome msg.
    if frontend.chat_interface.objects:
        welcome = frontend.chat_interface.objects[0]
        frontend.chat_interface.objects = [welcome]

    # VFS tree browser (sidebar file manager)
    pending_uploads: list[str] = []
    tree_browser = VFSTreeBrowser(_vfs, preload_depth=2)

    def _preview_entry(entry):
        """Show file preview in panelini's preview pane."""
        import base64 as b64mod

        mime = entry.mime.split(";")[0].strip()
        data_b64 = b64mod.b64encode(entry.blob).decode()

        if mime.startswith("image/"):
            frontend.preview_content.object = (
                f"# {entry.filename}\n\n"
                f'<img src="data:{mime};base64,{data_b64}" '
                f'alt="{entry.filename}" '
                f'style="max-width:100%;max-height:100%;'
                f'height:auto;object-fit:contain;">'
            )
        elif mime == "application/pdf":
            frontend.preview_content.object = (
                f"# {entry.filename}\n\n"
                f'<iframe src="data:application/pdf;base64,'
                f'{data_b64}" '
                f'style="width:100%;height:600px;border:none;">'
                f"</iframe>"
            )
        elif mime == "text/html":
            html = entry.blob.decode("utf-8", errors="replace")
            escaped = html.replace('"', "&quot;")
            frontend.preview_content.object = (
                f"# {entry.filename}\n\n"
                f'<iframe srcdoc="{escaped}" '
                f'sandbox="allow-same-origin" '
                f'style="width:100%;height:600px;'
                f'border:1px solid #ccc;"></iframe>'
            )
        elif mime == "text/markdown" or entry.filename.endswith(
            ".md",
        ):
            frontend.preview_content.object = (
                entry.blob.decode("utf-8", errors="replace")
            )
        else:
            frontend.preview_content.object = (
                f"# {entry.filename}\n\n"
                f"**Type:** {mime}  \n"
                f"**Size:** {human_size(entry.size)}"
                f"\n\n*Preview not available. Use download.*"
            )

    # Wire tree activation to preview pane by wrapping
    # the tree browser's on_tree_event method.
    _orig_on_tree_event = tree_browser.on_tree_event

    def _on_tree_event(event_name, event_params):
        _orig_on_tree_event(event_name, event_params)
        if event_name == "activate":
            uri = event_params.get("key", "")
            if uri and not _vfs.isdir(uri):
                _, rel = _vfs.parse_uri(uri)
                fn = rel.rsplit("/", 1)[-1] if "/" in rel else rel
                entry = _registry.get(fn)
                if entry:
                    _preview_entry(entry)

    tree_browser.on_tree_event = _on_tree_event

    # File upload from tree browser adds to registry
    orig_upload = tree_browser._on_file_upload

    def _upload_and_register(event):
        """Intercept tree upload to also register in middleware."""
        if event.new is not None:
            blob = event.new
            fn = tree_browser.file_input.filename or "upload"
            mime = (
                tree_browser.file_input.mime_type
                or "application/octet-stream"
            )
            # Register in middleware (also writes to VFS)
            entry = _registry.register(fn, bytes(blob), mime, "upload")
            note = f"{entry.filename} ({human_size(entry.size)})"
            pending_uploads.append(note)
            logger.info("File registered via tree: %s", note)
            frontend.chat_interface.send(
                f"File received: **{note}**. "
                f"You can now ask me to process it.",
                user="System",
                respond=False,
            )
            # Refresh tree to show new file
            tree_browser.tree.source = tree_browser.build_source()
            return  # Skip original handler (we already wrote to VFS)
        orig_upload(event)

    # Add our upload handler. The original VFSTreeBrowser
    # handler also fires but our early return prevents
    # double-writing to VFS.
    tree_browser.file_input.param.watch(
        _upload_and_register, "value",
    )

    # Real-time file notifications from tools
    def _on_file_produced(entry):
        widget = render_file_notification(
            entry, on_preview=_preview_entry,
        )
        frontend.chat_interface.send(
            widget,
            user="System",
            respond=False,
        )
        # Refresh tree to show new file
        tree_browser.tree.source = tree_browser.build_source()

    set_file_notify_callback(_on_file_produced)

    # Refresh tree after filesystem-agent MCP tools modify VFS
    def _on_vfs_changed():
        tree_browser.tree.source = tree_browser.build_source()

    set_vfs_changed_callback(_on_vfs_changed)

    # Wrap chat callback
    frontend.chat_interface.callback = _wrap_chat_callback(
        frontend.chat_interface.callback,
        _registry,
        pending_uploads,
        None,  # no FileManager - tree refreshes via callbacks
    )

    # Compose Panelini layout
    app = Panelini(title="Semos Agentura", sidebar_enabled=True)
    app.sidebar_set(
        objects=frontend.sidebar_objects + [
            pn.Card(
                tree_browser.tree,
                tree_browser.file_input,
                tree_browser.status,
                title="Files",
                collapsed=False,
            ),
        ],
    )
    app.main_set(objects=frontend.main_objects)
    return app


def main() -> None:
    """Launch the Agentura UI."""
    load_dotenv(_UI_DIR / ".env")
    load_dotenv(_UI_DIR.parent / ".env")

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
    )

    # 1. Register litellm provider (sync, before anything)
    _register_litellm_provider()

    # 2. Start filesystem-agent in-process with shared VFS.
    # It runs as a uvicorn thread so MCP tools connect normally.
    global _fs_agent_port
    _fs_agent_port = int(os.environ.get("FILESYSTEM_AGENT_PORT", "8003"))
    _start_filesystem_agent_inprocess(_vfs, _fs_agent_port)

    agents = _build_agents()

    # 3. Discover MCP tools (sync - connect, list, disconnect)
    global _hub
    _hub = MCPHub(agents)
    try:
        asyncio.run(_hub.discover())
    except Exception:
        logger.exception(
            "MCP discovery failed. Continuing without MCP tools.",
        )

    # 4. Create MCP tool wrappers (structured, schema-validated)
    from .mcp_tools import create_mcp_tools

    mcp_tools = create_mcp_tools(_hub, _registry)

    # 5. Discover A2A agents and create delegate tools
    # (natural language, agent routes internally)
    base_urls = [a.base_url for a in agents]
    delegates: list = []
    try:
        a2a_agents = asyncio.run(discover_agents(base_urls))
        delegates = create_a2a_delegates(a2a_agents, _registry)
    except Exception:
        logger.exception(
            "A2A discovery failed. Continuing without delegates.",
        )

    # 6. Register all tools in panelini
    all_tools = mcp_tools + delegates
    AVAILABLE_TOOLS.extend(all_tools)
    logger.info(
        "Registered %d tools (%d MCP + %d delegates): %s",
        len(all_tools),
        len(mcp_tools),
        len(delegates),
        [t.name for t in all_tools],
    )

    # 7. Start Panel server
    pn.extension(sizing_mode="stretch_width")
    port = int(os.environ.get("UI_PORT", "5006"))
    pn.serve(
        create_app,
        port=port,
        title="Semos Agentura",
        show=True,
        websocket_max_message_size=100 * 1024 * 1024,  # 100 MB
        keep_alive_milliseconds=30000,
        check_unused_sessions_milliseconds=60000,
        unused_session_lifetime_milliseconds=3600000,  # 1h
    )


if __name__ == "__main__":
    main()
