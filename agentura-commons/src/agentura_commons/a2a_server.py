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
            files.append(
                {
                    "name": name,
                    "content": f"data:{mime};base64,{b64}",
                }
            )
        elif part.HasField("url"):
            files.append(
                {
                    "name": part.filename or "attachment",
                    "content": part.url,
                }
            )
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


_STATUS_MAP = {
    "completed": TaskState.TASK_STATE_COMPLETED,
    "input_required": TaskState.TASK_STATE_INPUT_REQUIRED,
    "rejected": TaskState.TASK_STATE_REJECTED,
    "auth_required": TaskState.TASK_STATE_AUTH_REQUIRED,
    "failed": TaskState.TASK_STATE_FAILED,
}

# In-memory store for conversation state.
# task_id -> history (for LLM continuation)
_task_histories: dict[str, list[dict]] = {}
# context_id -> files (shared across tasks in same conversation)
_context_files: dict[str, list[dict]] = {}


class _AgentExecutor(AgentExecutor):
    """Routes A2A requests to BaseAgentService tools."""

    def __init__(self, service: BaseAgentService) -> None:
        self._service = service

    async def execute(
        self,
        context: RequestContext,
        event_queue: EventQueue,
    ) -> None:
        task_id = context.task_id or "unknown"
        ctx_id = context.context_id or "unknown"

        def _msg(text: str) -> Message:
            return Message(
                role=Role.ROLE_AGENT,
                parts=[Part(text=text)],
            )

        # Signal working
        await event_queue.enqueue_event(
            TaskStatusUpdateEvent(
                task_id=task_id,
                context_id=ctx_id,
                status=TaskStatus(state=TaskState.TASK_STATE_WORKING),
            )
        )

        try:
            result = await self._dispatch(context)
        except Exception as e:
            logger.exception("A2A execute failed")
            await event_queue.enqueue_event(
                TaskStatusUpdateEvent(
                    task_id=task_id,
                    context_id=ctx_id,
                    status=TaskStatus(
                        state=TaskState.TASK_STATE_FAILED,
                        message=_msg(str(e)),
                    ),
                )
            )
            return

        # Emit progress updates
        for update in result.progress_updates:
            await event_queue.enqueue_event(
                TaskStatusUpdateEvent(
                    task_id=task_id,
                    context_id=ctx_id,
                    status=TaskStatus(
                        state=TaskState.TASK_STATE_WORKING,
                        message=_msg(update),
                    ),
                )
            )

        # Emit file artifacts
        for f in result.files:
            await event_queue.enqueue_event(
                TaskArtifactUpdateEvent(
                    task_id=task_id,
                    context_id=ctx_id,
                    artifact=Artifact(
                        name=f.get("filename", ""),
                        parts=[
                            Part(
                                url=f.get("download_url", ""),
                                filename=f.get("filename", ""),
                                media_type=f.get("mime_type", ""),
                            )
                        ],
                    ),
                ),
            )

        # Manage conversation state for multi-turn
        # History is per task_id, files are per context_id
        if result.status in ("input_required", "auth_required"):
            _task_histories[task_id] = result.history

        # Accumulate files in context (persist across tasks)
        new_files = _extract_files(context.message)
        if new_files and ctx_id:
            prev = _context_files.get(ctx_id, [])
            seen = {f.get("name") for f in prev}
            for f in new_files:
                if f.get("name") not in seen:
                    prev.append(f)
            _context_files[ctx_id] = prev

        if result.status not in (
            "input_required",
            "auth_required",
        ):
            _task_histories.pop(task_id, None)

        # Emit final status
        state = _STATUS_MAP.get(
            result.status,
            TaskState.TASK_STATE_COMPLETED,
        )
        await event_queue.enqueue_event(
            TaskStatusUpdateEvent(
                task_id=task_id,
                context_id=ctx_id,
                status=TaskStatus(
                    state=state,
                    message=_msg(result.text),
                ),
            )
        )

    async def _dispatch(self, context: RequestContext):
        """Route request and return ExecutorResult."""
        from .llm_executor import ExecutorResult

        message = context.message

        # 1. Explicit tool call via DataPart
        tool_call = _extract_tool_call(message)
        if tool_call:
            tool_name, args = tool_call
            files = _extract_files(message)
            text, file_list = await self._call_tool(
                tool_name,
                args,
                files,
            )
            return ExecutorResult(
                text=text,
                files=file_list,
                status="completed",
            )

        # 2. Natural language
        text = _extract_text(message)
        files = _extract_files(message)

        # Try LLM executor if configured
        if self._service.router_llm_model:
            result = await self._run_llm_executor(
                text,
                files,
                context.task_id,
                context_id=context.context_id,
            )
            if result:
                return result

        # 3. Fallback: delegate to execute_skill
        skill_result = await self._service.execute_skill(
            skill_id=(self._service.get_skills()[0].id if self._service.get_skills() else "default"),
            message=text,
            task_id=context.task_id,
        )
        return ExecutorResult(text=skill_result, status="completed")

    async def _run_llm_executor(
        self,
        text: str,
        files: list[dict],
        task_id: str | None,
        context_id: str | None = None,
    ):
        """Run multi-step LLM executor. Returns ExecutorResult or None."""
        from .llm_executor import LLMExecutor

        try:
            # Retrieve history for task continuation
            history = None
            if task_id and task_id in _task_histories:
                history = _task_histories[task_id]

            # Merge context files (persist across tasks)
            if context_id and context_id in _context_files:
                seen = {f.get("name") for f in files}
                for cf in _context_files[context_id]:
                    if cf.get("name") not in seen:
                        files.append(cf)

            executor = LLMExecutor(
                tools=self._service.get_tools(),
                model=self._service.router_llm_model,
                api_key=self._service.router_llm_api_key,
                api_base=self._service.router_llm_api_base,
            )
            return await executor.run(
                text,
                files=files,
                history=history,
            )
        except Exception as exc:
            logger.exception(
                "LLM executor failed: %s",
                exc,
            )
            from .llm_executor import ExecutorResult

            return ExecutorResult(
                text=f"LLM executor error: {exc}",
                status="failed",
            )

    async def _call_tool(
        self,
        name: str,
        args: dict,
        files: list[dict] | None = None,
    ) -> tuple[str, list[dict]]:
        """Call a tool by name, return (text, file_list)."""
        tool_defs = {t.name: t for t in self._service.get_tools()}
        td = tool_defs.get(name)
        if not td:
            return f"Unknown tool: {name}", []

        if files and td.file_params:
            by_name = {f["name"]: f["content"] for f in files if f.get("name") and f.get("content")}
            for param in td.file_params:
                val = args.get(param)
                if isinstance(val, str) and val in by_name:
                    args[param] = {
                        "name": val,
                        "content": by_name[val],
                    }

        result = await td.fn(**args)
        return self._parse_tool_result(result)

    def _parse_tool_result(
        self,
        result: Any,
    ) -> tuple[str, list[dict]]:
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

    async def cancel(
        self,
        context: RequestContext,
        event_queue: EventQueue,
    ) -> None:
        task_id = context.task_id or "unknown"
        _task_histories.pop(task_id, None)
        await event_queue.enqueue_event(
            TaskStatusUpdateEvent(
                task_id=task_id,
                context_id=context.context_id or "unknown",
                status=TaskStatus(
                    state=TaskState.TASK_STATE_CANCELED,
                ),
            )
        )


def create_agent_card(
    service: BaseAgentService,
    base_url: str = "http://localhost:8000",
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
