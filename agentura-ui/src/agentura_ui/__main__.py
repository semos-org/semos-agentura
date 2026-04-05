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
from panelini.components.ai.frontend import Frontend
from panelini.components.ai.tools.basic_tools import AVAILABLE_TOOLS
from panelini.components.ai.utils.ai_interface import (
    PROVIDER_CLASS_REGISTRY,
)

from .a2a_client import A2AAgentInfo, discover_agents
from .a2a_tools import create_a2a_tools
from .agent_registry import AgentRegistry, Protocol
from .file_manager import FileManager
from .file_registry import FileRegistry, human_size
from .mcp_hub import AgentConnection, MCPHub
from .mcp_tools import drain_produced_files, set_status_callback
from .renderers import render_file_entry, resolve_file_references

logger = logging.getLogger(__name__)

_PKG_DIR = Path(__file__).resolve().parent
_UI_DIR = _PKG_DIR.parent.parent  # agentura-ui/
_CONFIG_YML = _UI_DIR / "config.yml"

_SYSTEM_MESSAGE = """\
You are a helpful assistant with access to email and document \
processing tools.

IMPORTANT - file handling:
- When the user uploads a file, it is stored locally. You will \
see a message like "I have uploaded a file: report.pdf (240 KB)".
- To process an uploaded file, call the appropriate tool and pass \
the EXACT filename as the source or file_path parameter. \
Example: digest_document(source="report.pdf")
- The system automatically resolves filenames to file content - \
never ask for file paths or base64. Just use the filename.
- Always call the tool immediately when the user asks to process \
an uploaded file.

Available tool groups:
- Email: search, read, draft, reply, calendar events
- Documents: digest (OCR), compose (PDF/PPTX/DOCX/HTML), \
diagrams, form inspect/fill
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
                "EMAIL_AGENT_BASE", "http://localhost:8001",
            ),
        ),
        AgentConnection(
            name="document-agent",
            url=os.environ.get(
                "DOCUMENT_AGENT_URL",
                "http://localhost:8002/mcp/sse",
            ),
            base_url=os.environ.get(
                "DOCUMENT_AGENT_BASE", "http://localhost:8002",
            ),
        ),
    ]


def _wrap_chat_callback(original_callback, registry,
                        pending_uploads, file_mgr):
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
            contents = (
                f"[Uploaded files available: {file_list}]\n\n"
                + contents
            )
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
        new_files = drain_produced_files()
        resolved_any = False
        if last_chunk and isinstance(last_chunk, str):
            resolved = resolve_file_references(
                last_chunk, registry,
            )
            if resolved != last_chunk:
                resolved_any = True
                yield resolved

        # Notify about produced files + show download widget
        # for non-image files. Images already rendered inline
        # are skipped as widgets but still announced.
        for entry in new_files:
            instance.send(
                f"File created: **{entry.filename}** "
                f"({human_size(entry.size)})",
                user="System", respond=False,
            )
            # Show download/preview widget for non-images,
            # or images that weren't resolved inline.
            if not (resolved_any
                    and entry.mime.startswith("image/")):
                widget = render_file_entry(entry)
                instance.send(
                    widget, user="System", respond=False,
                )
        if new_files:
            file_mgr.refresh()

    return wrapped


# Module-level state (single-user app)
_hub: MCPHub | None = None
_registry = FileRegistry()
_agent_registry: AgentRegistry | None = None


def _build_agent_section(registry, frontend):
    """Build the Agents sidebar card with protocol selectors."""
    if registry is None:
        return None
    agents = registry.get_agents()
    if not agents:
        return None

    rows = []
    for agent in agents:
        # Agent name + description tooltip
        label = pn.pane.HTML(
            f'<span title="{agent.description}">'
            f"<b>{agent.display_name}</b></span>",
            sizing_mode="stretch_width",
            margin=(5, 0),
        )

        # Enable/disable
        enable_cb = pn.widgets.Checkbox(
            value=agent.enabled,
            width=20,
            margin=(5, 5, 5, 0),
        )

        # Protocol availability indicators + selector
        options = {}
        if agent.mcp_available:
            options["MCP"] = "mcp"
        if agent.a2a_available:
            options["A2A"] = "a2a"

        if len(options) > 1:
            proto_select = pn.widgets.Select(
                options=options,
                value=agent.active_protocol.value,
                width=70, height=28,
                margin=(2, 0),
            )

            def _on_proto(event, name=agent.name):
                registry.set_protocol(name, Protocol(event.new))
                _swap_tools(registry, frontend, name)

            proto_select.param.watch(_on_proto, "value")
        else:
            proto_label = list(options.keys())[0] if options else "-"
            proto_select = pn.pane.HTML(
                f"<small>{proto_label}</small>",
                width=40,
                margin=(5, 0),
            )

        def _on_enable(event, name=agent.name):
            registry.set_enabled(name, event.new)
            _toggle_agent_tools(frontend, registry, name)

        enable_cb.param.watch(_on_enable, "value")

        rows.append(pn.Row(
            enable_cb, label, proto_select,
            sizing_mode="stretch_width",
        ))

    return pn.Card(
        *rows, title="Agents", collapsed=False,
        styles={"padding": "8px", "margin-bottom": "10px"},
    )


def _swap_tools(registry, frontend, agent_name):
    """Swap tool instances when protocol changes for an agent."""
    agent = registry.get_agent(agent_name)
    if not agent:
        return
    if agent.active_protocol == Protocol.A2A:
        new_tools = agent.a2a_tools
    else:
        new_tools = agent.mcp_tools
    # Swap in panelini's checkbox dict and ensure checked
    for tool_name, tool in new_tools.items():
        if tool_name in frontend.tool_checkboxes:
            frontend.tool_checkboxes[tool_name]["tool"] = tool
            frontend.tool_checkboxes[tool_name]["checkbox"].value = True
    # Trigger backend update with all selected tools
    frontend.backend.update_tools(frontend._get_selected_tools())
    logger.info(
        "Swapped %s tools to %s (%d tools)",
        agent_name, agent.active_protocol.value,
        len(new_tools),
    )


def _toggle_agent_tools(frontend, registry, agent_name):
    """Enable/disable all tool checkboxes for an agent."""
    agent = registry.get_agent(agent_name)
    if not agent:
        return
    all_names = set(agent.mcp_tools) | set(agent.a2a_tools)
    for name in all_names:
        if name in frontend.tool_checkboxes:
            frontend.tool_checkboxes[name]["checkbox"].value = (
                agent.enabled
            )


def create_app() -> Panelini:
    """Per-session app factory called by pn.serve."""
    frontend = Frontend(
        system_message=_SYSTEM_MESSAGE,
        config_path=_CONFIG_YML,
    )

    # Connect tool status updates to the chat placeholder.
    def _on_status(text):
        frontend.chat_interface.placeholder_text = text

    set_status_callback(_on_status)

    # Enable only delegate tools (ask_*) by default.
    for name, info in frontend.tool_checkboxes.items():
        info["checkbox"].value = name.startswith("ask_")
    frontend.backend.update_tools(frontend._get_selected_tools())
    logger.info(
        "Default tools: %s",
        [n for n, i in frontend.tool_checkboxes.items()
         if i["checkbox"].value],
    )
    # Clear the "Tools updated" spam, keep only the welcome msg.
    if frontend.chat_interface.objects:
        welcome = frontend.chat_interface.objects[0]
        frontend.chat_interface.objects = [welcome]

    # File manager (sidebar)
    pending_uploads: list[str] = []

    def _preview(entry):
        """Show file preview in panelini's preview pane.

        Natively supports: markdown, images, PDF, HTML.
        Other formats show metadata (a future generate_preview
        tool could convert e.g. DOCX to PDF for preview).
        """
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
            # Embedded PDF viewer via iframe + data URI
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
            # Unsupported format - show metadata.
            # TODO: add generate_preview tool that converts
            # DOCX/PPTX/XLSX to PDF via document-agent's
            # compose_document, then preview the PDF.
            frontend.preview_content.object = (
                f"# {entry.filename}\n\n"
                f"**Type:** {mime}  \n"
                f"**Size:** {human_size(entry.size)}"
                f"\n\n*Preview not available for this "
                f"format. Use download to open.*"
            )

    def _chat_notify(msg):
        frontend.chat_interface.send(
            msg, user="System", respond=False,
        )

    file_mgr = FileManager(
        _registry,
        pending_uploads,
        on_preview=_preview,
        on_chat_notify=_chat_notify,
    )

    # Wrap chat callback
    frontend.chat_interface.callback = _wrap_chat_callback(
        frontend.chat_interface.callback,
        _registry,
        pending_uploads,
        file_mgr,
    )

    # Agents sidebar section (protocol selection)
    agent_card = _build_agent_section(
        _agent_registry, frontend,
    )

    # Compose Panelini layout
    app = Panelini(title="Semos Agentura", sidebar_enabled=True)
    sidebar = frontend.sidebar_objects
    # Insert Agents card before the tools section
    if agent_card:
        sidebar = [agent_card] + sidebar
    app.sidebar_set(objects=sidebar + [file_mgr.panel])
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

    agents = _build_agents()

    # 2. Discover MCP tools (sync - connect, list, disconnect)
    global _hub, _agent_registry
    _hub = MCPHub(agents)
    try:
        asyncio.run(_hub.discover())
    except Exception:
        logger.exception(
            "MCP discovery failed. Continuing without MCP tools.",
        )

    # 3. Discover A2A agents (Agent Cards)
    base_urls = [a.base_url for a in agents]
    a2a_agents: list[A2AAgentInfo] = []
    try:
        a2a_agents = asyncio.run(discover_agents(base_urls))
    except Exception:
        logger.exception(
            "A2A discovery failed. Continuing without A2A.",
        )

    # 4. Build unified agent registry
    _agent_registry = AgentRegistry(
        default_protocol=Protocol(
            os.environ.get("DEFAULT_PROTOCOL", "mcp"),
        ),
    )

    # Register agents and MCP tools
    mcp_tools_by_agent: dict[str, list] = {}
    for conn in agents:
        _agent_registry.register_agent(
            conn.name, conn.name, "", conn.base_url,
        )
        # Collect MCP tools for this agent
        agent_mcp_tools = [
            t for t in _hub.all_tools()
            if _hub.agent_for_tool(t.name).name == conn.name
        ]
        if agent_mcp_tools:
            from .mcp_tools import _make_mcp_tool_class
            wrappers = {}
            meta = {}
            for t in agent_mcp_tools:
                wrappers[t.name] = _make_mcp_tool_class(
                    t, _hub, _registry,
                )
                meta[t.name] = t
            _agent_registry.set_mcp_tools(
                conn.name, wrappers, meta,
            )
            mcp_tools_by_agent[conn.name] = agent_mcp_tools

    # Register A2A tools (reuse MCP tool schemas)
    a2a_by_base = {a.base_url: a for a in a2a_agents}
    for conn in agents:
        a2a_info = a2a_by_base.get(conn.base_url)
        if not a2a_info:
            continue
        # Update display name from A2A card
        _agent_registry.register_agent(
            conn.name, a2a_info.name,
            a2a_info.description, conn.base_url,
        )
        # Create A2A wrappers using MCP tool schemas
        agent_mcp = mcp_tools_by_agent.get(conn.name, [])
        if agent_mcp:
            a2a_wrappers, delegate = create_a2a_tools(
                a2a_info, agent_mcp, _registry,
            )
            _agent_registry.set_a2a_tools(
                conn.name, a2a_wrappers, delegate,
            )

    # Auto-select protocol per agent based on tool annotations
    _agent_registry.auto_select_protocols()

    # 5. Register active tools in panelini
    active_tools = _agent_registry.get_active_tools()
    AVAILABLE_TOOLS.extend(active_tools)
    logger.info(
        "Registered %d tools (active): %s",
        len(active_tools),
        [t.name for t in active_tools],
    )

    # 4. Start Panel server
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
