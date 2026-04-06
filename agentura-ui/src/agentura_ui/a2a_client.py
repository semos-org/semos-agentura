"""A2A client - agent discovery and task execution via a2a-sdk 1.0."""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass, field

import httpx
from a2a.client import (
    A2ACardResolver,
    ClientConfig,
    ClientFactory,
    create_text_message_object,
)
from a2a.client.client import Client
from a2a.types import a2a_pb2
from google.protobuf import struct_pb2

logger = logging.getLogger(__name__)


@dataclass
class FileInfo:
    """File metadata extracted from an A2A artifact."""

    url: str
    filename: str
    mime_type: str = "application/octet-stream"


@dataclass
class A2AAgentInfo:
    """Discovered A2A agent with its card and client."""

    name: str
    description: str
    base_url: str
    card: a2a_pb2.AgentCard | None = None
    skills: list = field(default_factory=list)
    _client: Client | None = field(
        default=None, repr=False,
    )


async def discover_agents(
    base_urls: list[str],
) -> list[A2AAgentInfo]:
    """Fetch Agent Cards from each base URL.

    Tolerates failures - offline agents are skipped.
    """
    agents: list[A2AAgentInfo] = []
    async with httpx.AsyncClient(timeout=10.0) as hc:
        for url in base_urls:
            try:
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
                    "A2A discovered %s at %s: %d skills",
                    card.name, url, len(card.skills),
                )
                agents.append(info)
            except Exception:
                logger.warning(
                    "A2A discovery failed for %s", url,
                )
    return agents


def _ensure_client(agent: A2AAgentInfo) -> Client:
    """Create or return the a2a-sdk client for an agent."""
    if agent._client is not None:
        return agent._client
    hc = httpx.AsyncClient(timeout=300.0)  # 5 min for long ops
    config = ClientConfig(streaming=False, httpx_client=hc)
    factory = ClientFactory(config)
    agent._client = factory.create(agent.card)
    return agent._client


def _extract_result(
    task: a2a_pb2.Task | None,
    stream_files: list[FileInfo],
) -> tuple[str, list[FileInfo]]:
    """Extract text + files from a completed task."""
    if task is None:
        return "No response from agent.", stream_files

    # Text from status message
    text_parts = []
    if task.status and task.status.message:
        for part in task.status.message.parts:
            if part.text:
                text_parts.append(part.text)

    # Files from task artifacts (deduplicate with stream files)
    files = list(stream_files)
    seen = {f.url for f in files}
    for artifact in task.artifacts:
        for part in artifact.parts:
            if part.url and part.url not in seen:
                files.append(FileInfo(
                    url=part.url,
                    filename=(
                        part.filename or artifact.name or ""
                    ),
                    mime_type=(
                        part.media_type
                        or "application/octet-stream"
                    ),
                ))
                seen.add(part.url)

    if files:
        logger.info(
            "A2A result: %d files: %s",
            len(files),
            [f.filename for f in files],
        )

    return "\n".join(text_parts) or "Done.", files


async def _send_and_collect(
    client: Client,
    request: a2a_pb2.SendMessageRequest,
) -> tuple[str, list[FileInfo]]:
    """Send message and collect task result + artifacts."""
    task: a2a_pb2.Task | None = None
    files: list[FileInfo] = []

    async for stream_resp, t in client.send_message(request):
        if t is not None:
            task = t
        # Collect artifacts from stream events
        if (stream_resp.artifact_update
                and stream_resp.artifact_update.artifact):
            art = stream_resp.artifact_update.artifact
            for part in art.parts:
                if part.url:
                    files.append(FileInfo(
                        url=part.url,
                        filename=(
                            part.filename or art.name or ""
                        ),
                        mime_type=(
                            part.media_type
                            or "application/octet-stream"
                        ),
                    ))

    return _extract_result(task, files)


async def send_tool_call(
    agent: A2AAgentInfo,
    tool_name: str,
    arguments: dict,
) -> tuple[str, list[FileInfo]]:
    """Invoke a specific tool on an agent via A2A.

    Sends a DataPart with {"tool": name, "arguments": {...}}.
    """
    client = _ensure_client(agent)
    s = struct_pb2.Struct()
    s.update({"tool": tool_name, "arguments": arguments})
    data_value = struct_pb2.Value(struct_value=s)
    msg = a2a_pb2.Message(
        message_id=uuid.uuid4().hex,
        role=a2a_pb2.Role.ROLE_USER,
        parts=[a2a_pb2.Part(data=data_value)],
    )
    request = a2a_pb2.SendMessageRequest(message=msg)
    return await _send_and_collect(client, request)


async def send_task(
    agent: A2AAgentInfo,
    text: str,
    file_parts: list[a2a_pb2.Part] | None = None,
) -> tuple[str, list[FileInfo]]:
    """Send a natural-language task to an agent via A2A.

    Optionally includes file parts (inline base64) alongside
    the text message.
    """
    client = _ensure_client(agent)
    parts = [a2a_pb2.Part(text=text)]
    if file_parts:
        parts.extend(file_parts)
    msg = a2a_pb2.Message(
        message_id=uuid.uuid4().hex,
        role=a2a_pb2.Role.ROLE_USER,
        parts=parts,
    )
    request = a2a_pb2.SendMessageRequest(message=msg)
    return await _send_and_collect(client, request)


async def close_all(agents: list[A2AAgentInfo]) -> None:
    """Close all A2A clients."""
    for agent in agents:
        if agent._client:
            try:
                agent._client.close()
            except Exception:
                pass
            agent._client = None
