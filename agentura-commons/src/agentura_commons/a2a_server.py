"""Create an A2A server from a BaseAgentService."""

from __future__ import annotations

from typing import TYPE_CHECKING

from a2a.server.agent_execution import AgentExecutor, RequestContext
from a2a.server.events import EventQueue
from a2a.server.request_handlers import DefaultRequestHandler
from a2a.server.tasks import InMemoryTaskStore
from a2a.types import (
    AgentCapabilities,
    AgentCard,
    AgentSkill,
    Message,
    Part,
    Role,
    Task,
    TaskState,
    TaskStatus,
    TextPart,
)

if TYPE_CHECKING:
    from .base import BaseAgentService


class _AgentExecutorAdapter(AgentExecutor):
    """Adapts a BaseAgentService to the a2a-sdk AgentExecutor interface."""

    def __init__(self, service: BaseAgentService) -> None:
        self._service = service

    async def execute(self, context: RequestContext, event_queue: EventQueue) -> None:
        # Extract the user's text message
        user_message = ""
        if context.message and context.message.parts:
            for part in context.message.parts:
                if isinstance(part, TextPart) or (hasattr(part, "root") and hasattr(part.root, "text")):
                    text_part = part.root if hasattr(part, "root") else part
                    user_message += getattr(text_part, "text", "")

        # Determine which skill to execute (use first matching or default)
        skill_id = None
        if self._service.get_skills():
            skill_id = self._service.get_skills()[0].id

        # Execute the skill
        try:
            result = await self._service.execute_skill(
                skill_id=skill_id or "default",
                message=user_message,
                task_id=context.task_id,
            )
            # Publish completed task
            event_queue.enqueue_event(
                Task(
                    id=context.task_id or "unknown",
                    context_id=context.context_id or "unknown",
                    status=TaskStatus(
                        state=TaskState.completed,
                        message=Message(
                            role=Role.agent,
                            parts=[Part(TextPart(text=result))],
                        ),
                    ),
                )
            )
        except Exception as e:
            event_queue.enqueue_event(
                Task(
                    id=context.task_id or "unknown",
                    context_id=context.context_id or "unknown",
                    status=TaskStatus(
                        state=TaskState.failed,
                        message=Message(
                            role=Role.agent,
                            parts=[Part(TextPart(text=str(e)))],
                        ),
                    ),
                )
            )

    async def cancel(self, context: RequestContext, event_queue: EventQueue) -> None:
        event_queue.enqueue_event(
            Task(
                id=context.task_id or "unknown",
                context_id=context.context_id or "unknown",
                status=TaskStatus(state=TaskState.canceled),
            )
        )


def create_agent_card(service: BaseAgentService, base_url: str = "http://localhost:8000") -> AgentCard:
    """Build an A2A AgentCard from the agent service."""
    skills = [
        AgentSkill(
            id=s.id,
            name=s.name,
            description=s.description,
            tags=s.tags,
            examples=[{"name": ex, "description": ex} for ex in s.examples] if s.examples else [],
        )
        for s in service.get_skills()
    ]

    return AgentCard(
        name=service.agent_name,
        description=service.agent_description,
        version=service.agent_version,
        url=f"{base_url}/a2a",
        default_input_modes=["text/plain"],
        default_output_modes=["text/plain"],
        skills=skills,
        capabilities=AgentCapabilities(streaming=False),
    )


def create_a2a_handler(service: BaseAgentService) -> DefaultRequestHandler:
    """Build an A2A DefaultRequestHandler from the agent service."""
    executor = _AgentExecutorAdapter(service)
    task_store = InMemoryTaskStore()

    return DefaultRequestHandler(
        agent_executor=executor,
        task_store=task_store,
    )
