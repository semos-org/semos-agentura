"""A2A server: routes requests to MCP tools via BaseAgentService.

Uses a2a-sdk 1.0 protobuf types. Each agent exposes its tools as
A2A skills. Requests can be:
- Explicit tool call: Part(data={"tool": "name", "arguments": {...}})
- Natural language: Part(text="...") - routed via optional LLM router
"""

from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING, Any

from a2a.server.agent_execution import AgentExecutor, RequestContext
from a2a.server.events import EventQueue
from a2a.server.request_handlers import DefaultRequestHandler
from a2a.server.tasks import InMemoryTaskStore
from a2a.types import (
    AgentCapabilities,
    AgentCard,
    AgentInterface,
    AgentSkill,
    Artifact,
    Message,
    Part,
    Role,
    TaskArtifactUpdateEvent,
    TaskState,
    TaskStatus,
    TaskStatusUpdateEvent,
)

if TYPE_CHECKING:
    from .base import BaseAgentService

logger = logging.getLogger(__name__)


def _extract_text(message) -> str:
    """Extract text from message parts."""
    parts = []
    if message and message.parts:
        for part in message.parts:
            if part.HasField("text"):
                parts.append(part.text)
    return "\n".join(parts)


def _extract_files(message) -> list[dict]:
    """Extract file attachments from message parts.

    Returns list of FileAttachment dicts {name, content}.
    """
    files = []
    if not message or not message.parts:
        return files
    for part in message.parts:
        if part.HasField("raw"):
            import base64
            b64 = base64.b64encode(part.raw).decode()
            mime = part.media_type or "application/octet-stream"
            name = part.filename or "attachment.bin"
            files.append({
                "name": name,
                "content": f"data:{mime};base64,{b64}",
            })
        elif part.HasField("url"):
            files.append({
                "name": part.filename or "attachment",
                "content": part.url,
            })
    return files


def _extract_tool_call(message) -> tuple[str, dict] | None:
    """Check if message contains an explicit tool call via DataPart.

    Expected format: Part(data={"tool": "name", "arguments": {...}})
    """
    if not message or not message.parts:
        return None
    for part in message.parts:
        if part.HasField("data"):
            try:
                from google.protobuf.json_format import MessageToDict
                data = MessageToDict(part.data)
            except Exception:
                continue
            if isinstance(data, dict) and "tool" in data:
                return data["tool"], data.get("arguments", {})
    return None


class _AgentExecutor(AgentExecutor):
    """Routes A2A requests to BaseAgentService tools."""

    def __init__(self, service: BaseAgentService) -> None:
        self._service = service

    async def execute(
        self, context: RequestContext, event_queue: EventQueue,
    ) -> None:
        task_id = context.task_id or "unknown"
        ctx_id = context.context_id or "unknown"

        def _msg(text: str) -> Message:
            return Message(
                role=Role.ROLE_AGENT,
                parts=[Part(text=text)],
            )

        # Signal working
        await event_queue.enqueue_event(TaskStatusUpdateEvent(
            task_id=task_id, context_id=ctx_id,
            status=TaskStatus(state=TaskState.TASK_STATE_WORKING),
        ))

        try:
            result_text, result_files = await self._dispatch(context)
        except Exception as e:
            logger.exception("A2A execute failed")
            await event_queue.enqueue_event(TaskStatusUpdateEvent(
                task_id=task_id, context_id=ctx_id,
                status=TaskStatus(
                    state=TaskState.TASK_STATE_FAILED,
                    message=_msg(str(e)),
                ),
            ))
            return

        # Emit file artifacts
        for f in result_files:
            await event_queue.enqueue_event(
                TaskArtifactUpdateEvent(
                    task_id=task_id, context_id=ctx_id,
                    artifact=Artifact(
                        name=f.get("filename", ""),
                        parts=[Part(
                            url=f.get("download_url", ""),
                            filename=f.get("filename", ""),
                            media_type=f.get("mime_type", ""),
                        )],
                    ),
                ),
            )

        # Emit completed
        await event_queue.enqueue_event(TaskStatusUpdateEvent(
            task_id=task_id, context_id=ctx_id,
            status=TaskStatus(
                state=TaskState.TASK_STATE_COMPLETED,
                message=_msg(result_text),
            ),
        ))

    async def _dispatch(
        self, context: RequestContext,
    ) -> tuple[str, list[dict]]:
        """Route to tool and return (text, file_metadata_list)."""
        message = context.message

        # 1. Explicit tool call via DataPart
        tool_call = _extract_tool_call(message)
        if tool_call:
            tool_name, args = tool_call
            files = _extract_files(message)
            return await self._call_tool(tool_name, args, files)

        # 2. Natural language - try LLM routing if configured
        text = _extract_text(message)
        files = _extract_files(message)

        if self._service.router_llm_model:
            routed = await self._route_via_llm(text, files)
            if routed:
                return routed

        # 3. Fallback: delegate to execute_skill
        result = await self._service.execute_skill(
            skill_id=self._service.get_skills()[0].id
            if self._service.get_skills() else "default",
            message=text,
            task_id=context.task_id,
        )
        return result, []

    async def _call_tool(
        self, name: str, args: dict,
        files: list[dict] | None = None,
    ) -> tuple[str, list[dict]]:
        """Call a tool by name, return (text, file_list)."""
        tool_defs = {t.name: t for t in self._service.get_tools()}
        td = tool_defs.get(name)
        if not td:
            return f"Unknown tool: {name}", []

        # Inject file content into file params
        if files and td.file_params:
            by_name = {
                f["name"]: f["content"]
                for f in files if f.get("name") and f.get("content")
            }
            for param in td.file_params:
                val = args.get(param)
                if isinstance(val, str) and val in by_name:
                    args[param] = {
                        "name": val, "content": by_name[val],
                    }

        result = await td.fn(**args)
        return self._parse_tool_result(result)

    def _parse_tool_result(
        self, result: Any,
    ) -> tuple[str, list[dict]]:
        """Parse a tool's return value into (text, files)."""
        if result is None:
            return "", []
        text = str(result)
        try:
            data = json.loads(text)
        except (json.JSONDecodeError, TypeError):
            return text, []
        if isinstance(data, dict) and "download_url" in data:
            return text, [data]
        return text, []

    async def _route_via_llm(
        self, text: str, files: list[dict],
    ) -> tuple[str, list[dict]] | None:
        """Route natural language to a tool via LLM."""
        try:
            from .llm_router import route_to_tool
            result = await route_to_tool(
                text,
                self._service.get_tools(),
                model=self._service.router_llm_model,
                api_key=self._service.router_llm_api_key,
                api_base=self._service.router_llm_api_base,
            )
            if result:
                tool_name, args = result
                return await self._call_tool(tool_name, args, files)
        except Exception:
            logger.exception("LLM routing failed")
        return None

    async def cancel(
        self, context: RequestContext, event_queue: EventQueue,
    ) -> None:
        await event_queue.enqueue_event(TaskStatusUpdateEvent(
            task_id=context.task_id or "unknown",
            context_id=context.context_id or "unknown",
            status=TaskStatus(
                state=TaskState.TASK_STATE_CANCELED,
            ),
        ))


def create_agent_card(
    service: BaseAgentService, base_url: str = "http://localhost:8000",
) -> AgentCard:
    """Build an A2A AgentCard from the agent service."""
    skills = [
        AgentSkill(
            id=s.id,
            name=s.name,
            description=s.description,
            tags=s.tags,
            examples=[],
        )
        for s in service.get_skills()
    ]
    return AgentCard(
        name=service.agent_name,
        description=service.agent_description,
        version=service.agent_version,
        default_input_modes=["text/plain"],
        default_output_modes=["text/plain"],
        skills=skills,
        capabilities=AgentCapabilities(streaming=False),
        supported_interfaces=[
            AgentInterface(
                url=f"{base_url}/a2a",
                protocol_binding="HTTP+JSON",
            ),
            AgentInterface(
                url=f"{base_url}/a2a/rpc",
                protocol_binding="JSONRPC",
            ),
        ],
    )


def create_a2a_handler(
    service: BaseAgentService,
) -> DefaultRequestHandler:
    """Build an A2A DefaultRequestHandler."""
    executor = _AgentExecutor(service)
    task_store = InMemoryTaskStore()
    return DefaultRequestHandler(
        agent_executor=executor,
        task_store=task_store,
    )
