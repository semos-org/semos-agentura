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
from filesystem_agent.panel_tree import VFSTreeBrowser
from filesystem_agent.vfs import VirtualFileSystem
from panelini import Panelini
from panelini.panels.ai.frontend import AVAILABLE_TOOLS
from panelini.panels.ai.frontend import AiChat as Frontend
from panelini.panels.ai.utils.ai_interface import (
    PROVIDER_CLASS_REGISTRY,
)

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

DATES:
- If no year, month, or week is given, assume the current one \
by using the get_current_time tool first.

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


# Default agents. Additional agents can be added via
# EXTRA_AGENTS env var: "name:port,name:port,..."
_DEFAULT_AGENTS = [
    ("email-agent", 8001),
    ("document-agent", 8002),
    ("filesystem-agent", 8003),
]


def _build_agents() -> list[AgentConnection]:
    agents = []
    for name, port in _DEFAULT_AGENTS:
        env_key = name.upper().replace("-", "_")
        base = os.environ.get(
            f"{env_key}_BASE",
            f"http://localhost:{port}",
        )
        url = os.environ.get(
            f"{env_key}_URL",
            f"{base}/mcp/sse",
        )
        agents.append(
            AgentConnection(name=name, url=url, base_url=base),
        )

    # Extra agents from env: "survey-agent:8004,other:8005"
    extra = os.environ.get("EXTRA_AGENTS", "")
    for entry in extra.split(","):
        entry = entry.strip()
        if not entry:
            continue
        if ":" in entry:
            name, port_str = entry.rsplit(":", 1)
            base = f"http://localhost:{port_str}"
        else:
            name = entry
            base = "http://localhost:8080"
        agents.append(
            AgentConnection(
                name=name,
                url=f"{base}/mcp/sse",
                base_url=base,
            )
        )

    return agents


def _wrap_chat_callback(original_callback, registry, pending_uploads, _unused=None):
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
        logger.info("Callback returned type=%s", type(result).__name__)
        if hasattr(result, "__aiter__"):
            async for chunk in result:
                logger.info(
                    "Callback chunk: %d chars",
                    len(str(chunk)),
                )
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


def _build_tool_tree(frontend, hub):
    """Build a Wunderbaum tree with checkboxes for tool selection.

    Tools are grouped by agent. Delegate tools (ask_*) are
    checked by default. Checking/unchecking updates the backend.
    """
    from panelini.panels.wunderbaum import Wunderbaum

    # Group tools by agent. Tool names are prefixed
    # (email_agent__search_emails) but the hub knows
    # original MCP names (search_emails).
    groups: dict[str, list[str]] = {}
    for tool_name in frontend.tool_checkboxes:
        agent_label = "Built-in"
        # Strip agent prefix to look up in hub
        mcp_name = tool_name.split("__", 1)[1] if "__" in tool_name else tool_name
        if hub is not None:
            try:
                agent = hub.agent_for_tool(mcp_name)
                agent_label = agent.name
            except KeyError:
                pass
        if agent_label == "Built-in" and tool_name.startswith(
            "ask_",
        ):
            agent_label = "Agents"
        groups.setdefault(agent_label, []).append(tool_name)

    # Get tool descriptions for the grid column
    descs: dict[str, str] = {}
    for name, info in frontend.tool_checkboxes.items():
        descs[name] = getattr(
            info["tool"],
            "description",
            "",
        )[:80]

    # Ordered groups: Built-in first, then Agents, then MCP agents
    ordered = []
    for label in ["Built-in", "Agents"]:
        if label in groups:
            ordered.append((label, groups.pop(label)))
    for label in sorted(groups):
        ordered.append((label, groups[label]))

    # Build tree source. Column data at node root level
    # (not nested in "data" dict) per Wunderbaum convention.
    source = []
    default_selected = set()
    for agent_label, tool_names in ordered:
        children = []
        for tn in sorted(tool_names):
            is_default = tn.startswith("ask_") or tn == "get_current_time"
            icon = "bi bi-chat-dots" if tn.startswith("ask_") else "bi bi-wrench"
            # Display name: strip agent prefix, title-case
            display = (tn.split("__", 1)[1] if "__" in tn else tn).replace("_", " ").title()
            children.append(
                {
                    "key": tn,
                    "title": display,
                    "icon": icon,
                    "selected": is_default,
                    "desc": descs.get(tn, ""),
                }
            )
            if is_default:
                default_selected.add(tn)
        all_sel = all(tn in default_selected for tn in tool_names)
        folder_icon = "bi bi-robot" if agent_label == "Agents" else "bi bi-server"
        folder_title = agent_label.replace("-", " ").title()
        source.append(
            {
                "key": f"agent:{agent_label}",
                "title": folder_title,
                "icon": folder_icon,
                "expanded": True,
                "selected": all_sel,
                "children": children,
            }
        )

    def _get_checked_keys(src):
        """Return keys of all selected nodes (folders + leaves)."""
        keys = []

        def walk(nodes):
            for n in nodes:
                if n.get("selected"):
                    keys.append(n["key"])
                walk(n.get("children", []))

        walk(src)
        return keys

    def _sync_tools_from_source(*_args):
        """Sync panelini backend when tree checkboxes change.

        selectMode "hier" handles parent/child propagation in JS.
        We just read the resulting state and update the backend.
        """
        checked = set(_get_checked_keys(tree.source))
        # Only tool keys matter (skip folder keys like "agent:...")
        tool_keys = {k for k in checked if not k.startswith("agent:")}
        count = frontend.batch_update_tools(tool_keys)
        frontend.chat_interface.send(
            f"Tools updated. {count} tool(s) now available.",
            user="System",
            respond=False,
        )
        logger.info("Tools synced: %d checked", count)

    tree = Wunderbaum(
        source=source,
        columns=[
            {"id": "*", "title": "Tool", "width": "200px"},
            {
                "id": "desc",
                "title": "Description",
                "width": "*",
            },
        ],
        options={
            "checkbox": True,
            "selectMode": "hier",
        },
        sizing_mode="stretch_width",
        height=400,
    )

    # Set default tools on backend (single batch, no spam)
    frontend.batch_update_tools(default_selected)
    logger.info("Default tools: %s", sorted(default_selected))

    # Watch source changes (checkbox toggles sync source).
    # selectMode "hier" handles parent/child propagation in JS.
    tree.param.watch(_sync_tools_from_source, ["source"])
    return tree


def _start_filesystem_agent_inprocess(
    vfs: VirtualFileSystem,
    port: int,
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
                "Filesystem-agent port %d in use, skipping in-process start",
                port,
            )
            return

    from agentura_commons import create_app as create_agent_app

    service = FilesystemAgentService(vfs=vfs)
    agent_app = create_agent_app(
        service,
        base_url=f"http://127.0.0.1:{port}",
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
        "Filesystem-agent started in-process on port %d (shared VFS with %d roots)",
        port,
        len(vfs.roots),
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
            tc = response_data.get("tool_calls", [])
            logger.info(
                "Iteration %d: %d tool calls, text=%s",
                iteration,
                len(tc),
                str(response_data.get("text", ""))[:100],
            )
            if not tc:
                final = str(response_data.get("text", ""))
                logger.info(
                    "Tool loop done, returning %d chars",
                    len(final),
                )
                return final
            for t in tc:
                logger.info(
                    "  -> tool_call: %s(%s)",
                    t.get("name", "?"),
                    str(t.get("args", {}))[:100],
                )
            tool_results = await self._execute_tool_calls(tc)
            logger.info(
                "  <- tool results: %d messages",
                len(tool_results),
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

    # Build tool tree with Wunderbaum (grouped by agent).
    # Replaces panelini's flat checkbox list in the sidebar.
    tool_tree_widget = _build_tool_tree(frontend, _hub)

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
            frontend.preview_content.object = entry.blob.decode("utf-8", errors="replace")
        else:
            frontend.preview_content.object = (
                f"# {entry.filename}\n\n"
                f"**Type:** {mime}  \n"
                f"**Size:** {human_size(entry.size)}"
                f"\n\n*Preview not available. Use download.*"
            )

    # Wire tree activation to preview pane. Override the
    # Wunderbaum widget's callback directly (the VFSTreeBrowser
    # method reference was captured at construction time).
    _orig_tree_cb = tree_browser.tree._tree_event_callback

    def _on_file_tree_event(event_name, event_params):
        # Call original VFSTreeBrowser handler first
        if _orig_tree_cb:
            _orig_tree_cb(event_name, event_params)
        if event_name == "activate":
            uri = event_params.get("key", "")
            if uri and "://" in uri:
                try:
                    if not _vfs.isdir(uri):
                        _, rel = _vfs.parse_uri(uri)
                        fn = rel.rsplit("/", 1)[-1] if "/" in rel else rel
                        entry = _registry.get(fn)
                        if entry:
                            _preview_entry(entry)
                except Exception:
                    pass

    tree_browser.tree._tree_event_callback = _on_file_tree_event

    # File upload from tree browser adds to registry
    orig_upload = tree_browser._on_file_upload

    def _upload_and_register(event):
        """Intercept tree upload to also register in middleware."""
        if event.new is not None:
            blob = event.new
            fn = tree_browser.file_input.filename or "upload"
            mime = tree_browser.file_input.mime_type or "application/octet-stream"
            # Register in middleware (also writes to VFS)
            entry = _registry.register(fn, bytes(blob), mime, "upload")
            note = f"{entry.filename} ({human_size(entry.size)})"
            pending_uploads.append(note)
            logger.info("File registered via tree: %s", note)
            frontend.chat_interface.send(
                f"File received: **{note}**. You can now ask me to process it.",
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
        _upload_and_register,
        "value",
    )

    # Real-time file notifications from tools
    def _on_file_produced(entry):
        widget = render_file_notification(
            entry,
            on_preview=_preview_entry,
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

    # Compose Panelini layout.
    # Replace panelini's flat tool checkboxes with our tree.
    sidebar_objects = []
    for obj in frontend.sidebar_objects:
        # Find the "Basic Tools" card and replace its content
        if hasattr(obj, "objects"):
            replaced = False
            for i, child in enumerate(obj.objects):
                title = getattr(child, "title", "")
                if "tool" in title.lower():
                    obj.objects[i] = pn.Card(
                        tool_tree_widget,
                        title="Tools",
                        collapsed=False,
                        styles={
                            "margin-bottom": "12px",
                            "padding": "12px",
                        },
                    )
                    replaced = True
                    break
            if not replaced:
                sidebar_objects.append(obj)
            else:
                sidebar_objects.append(obj)
        else:
            sidebar_objects.append(obj)

    app = Panelini(
        title="Semos Agentura",
        sidebar_enabled=True,
        sidebars_max_width=450,
    )
    app.sidebar_set(
        objects=sidebar_objects
        + [
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
