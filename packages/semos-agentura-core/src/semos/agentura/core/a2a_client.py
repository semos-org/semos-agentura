"""A2A client - agent discovery, task execution, multi-turn support.

Protocol-level A2A client extracted from agentura-ui. No UI dependencies.
Uses a2a-sdk 1.0 protobuf types with REST (HTTP+JSON) binding.
"""

from __future__ import annotations

import base64
import logging
import uuid
from dataclasses import dataclass, field

import httpx
from a2a.client import (
    A2ACardResolver,
    ClientConfig,
    ClientFactory,
)
from a2a.client.client import Client
from a2a.types import a2a_pb2
from google.protobuf import struct_pb2

logger = logging.getLogger(__name__)

_REST_CONFIG = ClientConfig(
    streaming=False,
    supported_protocol_bindings=["HTTP+JSON"],
)


@dataclass
class FileInfo:
    """File metadata extracted from an A2A artifact."""

    url: str
    filename: str
    mime_type: str = "application/octet-stream"


@dataclass
class A2AResult:
    """Result from an A2A interaction.

    Extends (text, files) with task_id and status for multi-turn.
    """

    text: str = ""
    files: list[FileInfo] = field(default_factory=list)
    task_id: str | None = None
    context_id: str | None = None
    status: str = "completed"  # completed, input_required, rejected, auth_required, failed


@dataclass
class A2AAgentInfo:
    """Discovered A2A agent with its card and cached client."""

    name: str
    description: str
    base_url: str
    card: a2a_pb2.AgentCard | None = None
    skills: list = field(default_factory=list)
    _client: Client | None = field(default=None, repr=False)


# Task state to status string mapping
_STATE_TO_STATUS = {
    a2a_pb2.TaskState.TASK_STATE_COMPLETED: "completed",
    a2a_pb2.TaskState.TASK_STATE_FAILED: "failed",
    a2a_pb2.TaskState.TASK_STATE_INPUT_REQUIRED: "input_required",
    a2a_pb2.TaskState.TASK_STATE_REJECTED: "rejected",
    a2a_pb2.TaskState.TASK_STATE_AUTH_REQUIRED: "auth_required",
    a2a_pb2.TaskState.TASK_STATE_CANCELED: "canceled",
}


async def discover_agents(
    base_urls: dict[str, str] | list[str],
) -> dict[str, A2AAgentInfo] | list[A2AAgentInfo]:
    """Fetch Agent Cards from each base URL.

    Accepts either:
    - dict[name, url] -> returns dict[name, A2AAgentInfo]
    - list[url] -> returns list[A2AAgentInfo]

    Tolerates failures - offline agents are skipped.
    """
    is_dict = isinstance(base_urls, dict)
    items = base_urls.items() if is_dict else enumerate(base_urls)
    result_dict: dict[str, A2AAgentInfo] = {}
    result_list: list[A2AAgentInfo] = []

    for key, url in items:
        try:
            async with httpx.AsyncClient(timeout=10.0) as hc:
                resolver = A2ACardResolver(hc, url)
                card = await resolver.get_agent_card()
            info = A2AAgentInfo(
                name=card.name,
                description=card.description,
                base_url=url,
                card=card,
                skills=list(card.skills),
            )
            logger.info(
                "A2A discovered %s at %s (%d skills)",
                card.name,
                url,
                len(card.skills),
            )
            if is_dict:
                result_dict[key] = info
            else:
                result_list.append(info)
        except Exception:
            logger.warning("A2A discovery failed for %s", url)

    return result_dict if is_dict else result_list


def _ensure_client(agent: A2AAgentInfo) -> Client:
    """Create or return the a2a-sdk Client for an agent."""
    if agent._client is not None:
        return agent._client
    hc = httpx.AsyncClient(timeout=300.0)
    config = ClientConfig(
        streaming=False,
        httpx_client=hc,
        supported_protocol_bindings=["HTTP+JSON"],
    )
    factory = ClientFactory(config)
    agent._client = factory.create(agent.card)
    return agent._client


def _extract_result(
    task: a2a_pb2.Task | None,
    stream_files: list[FileInfo],
) -> A2AResult:
    """Extract A2AResult from a completed task."""
    if task is None:
        return A2AResult(
            text="No response from agent.",
            files=stream_files,
            status="failed",
        )

    # Text from status message
    text_parts = []
    if task.status and task.status.message:
        for part in task.status.message.parts:
            if part.text:
                text_parts.append(part.text)

    # Files from task artifacts (deduplicate with stream)
    files = list(stream_files)
    seen = {f.url for f in files}
    for artifact in task.artifacts:
        for part in artifact.parts:
            if part.url and part.url not in seen:
                files.append(
                    FileInfo(
                        url=part.url,
                        filename=part.filename or artifact.name or "",
                        mime_type=part.media_type or "application/octet-stream",
                    )
                )
                seen.add(part.url)

    # Map task state to status string
    status = "completed"
    if task.status:
        status = _STATE_TO_STATUS.get(
            task.status.state,
            "completed",
        )

    return A2AResult(
        text="\n".join(text_parts) or "Done.",
        files=files,
        task_id=task.id or None,
        context_id=task.context_id or None,
        status=status,
    )


async def _send_and_collect(
    client: Client,
    request: a2a_pb2.SendMessageRequest,
) -> A2AResult:
    """Send message and collect task result + artifacts."""
    task: a2a_pb2.Task | None = None
    files: list[FileInfo] = []

    async for stream_resp, t in client.send_message(request):
        if t is not None:
            task = t
        if stream_resp.artifact_update and stream_resp.artifact_update.artifact:
            art = stream_resp.artifact_update.artifact
            for part in art.parts:
                if part.url:
                    files.append(
                        FileInfo(
                            url=part.url,
                            filename=part.filename or art.name or "",
                            mime_type=part.media_type or "application/octet-stream",
                        )
                    )

    return _extract_result(task, files)


async def send_tool_call(
    agent: A2AAgentInfo,
    tool_name: str,
    arguments: dict,
    files: list[dict] | None = None,
) -> A2AResult:
    """Invoke a specific tool on an agent via A2A.

    Args:
        agent: Target agent.
        tool_name: Tool to call.
        arguments: Tool arguments dict.
        files: Optional file attachments [{name, content}].

    Returns:
        A2AResult with text, files, task_id, status.
    """
    client = _ensure_client(agent)
    s = struct_pb2.Struct()
    s.update({"tool": tool_name, "arguments": arguments})
    data_value = struct_pb2.Value(struct_value=s)
    parts = [a2a_pb2.Part(data=data_value)]

    # Attach files as inline base64
    for f in files or []:
        parts.append(_file_to_part(f))

    msg = a2a_pb2.Message(
        message_id=uuid.uuid4().hex,
        role=a2a_pb2.Role.ROLE_USER,
        parts=parts,
    )
    request = a2a_pb2.SendMessageRequest(message=msg)
    return await _send_and_collect(client, request)


async def send_task(
    agent: A2AAgentInfo,
    message: str,
    files: list[dict] | None = None,
    data: dict | None = None,
    task_id: str | None = None,
    context_id: str | None = None,
) -> A2AResult:
    """Send a natural-language task to an agent via A2A.

    Args:
        agent: Target agent.
        message: Natural language request.
        files: Optional file attachments [{name, content}].
        data: Optional structured data dict.
        task_id: Continue an existing task (for multi-turn).
        context_id: Conversation context ID.

    Returns:
        A2AResult with text, files, task_id, status.
    """
    client = _ensure_client(agent)
    parts: list[a2a_pb2.Part] = [a2a_pb2.Part(text=message)]

    # Attach files as inline base64
    for f in files or []:
        parts.append(_file_to_part(f))

    # Attach structured data
    if data:
        s = struct_pb2.Struct()
        s.update(data)
        parts.append(a2a_pb2.Part(data=struct_pb2.Value(struct_value=s)))

    msg = a2a_pb2.Message(
        message_id=uuid.uuid4().hex,
        role=a2a_pb2.Role.ROLE_USER,
        parts=parts,
        task_id=task_id or "",
        context_id=context_id or "",
    )
    request = a2a_pb2.SendMessageRequest(message=msg)
    return await _send_and_collect(client, request)


def _file_to_part(f: dict) -> a2a_pb2.Part:
    """Convert a FileAttachment dict to an A2A Part."""
    content = f.get("content", "")
    name = f.get("name", "file")
    mime = "application/octet-stream"

    if content.startswith("data:"):
        # data URI: extract mime and base64
        header, _, b64 = content.partition(",")
        if ";" in header:
            mime = header.split(":")[1].split(";")[0]
        raw = base64.b64decode(b64)
    elif isinstance(content, bytes):
        raw = content
    else:
        raw = content.encode("utf-8")

    return a2a_pb2.Part(
        raw=raw,
        filename=name,
        media_type=mime,
    )


async def close_all(
    agents: dict[str, A2AAgentInfo] | list[A2AAgentInfo],
) -> None:
    """Close all A2A clients."""
    items = agents.values() if isinstance(agents, dict) else agents
    for agent in items:
        if agent._client:
            try:
                await agent._client.close()
            except Exception:
                pass
            agent._client = None
