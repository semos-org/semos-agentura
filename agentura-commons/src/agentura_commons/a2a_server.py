"""Create an A2A server from a BaseAgentService.

NOTE: A2A support is pending upgrade to a2a-sdk 1.0 (Phase 3.5).
The current implementation is a stub that provides the create_agent_card
and create_a2a_handler functions needed by transport.py.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .base import BaseAgentService

logger = logging.getLogger(__name__)


def create_agent_card(service: BaseAgentService, base_url: str = "http://localhost:8000") -> dict:
    """Build an A2A AgentCard dict from the agent service.

    Returns a plain dict (JSON-serializable) instead of a protobuf/pydantic
    model to avoid a2a-sdk version coupling. The transport layer serves
    this at GET /.well-known/agent-card.json.
    """
    skills = [
        {
            "id": s.id,
            "name": s.name,
            "description": s.description,
            "tags": s.tags,
            "examples": [{"name": ex, "description": ex} for ex in s.examples] if s.examples else [],
        }
        for s in service.get_skills()
    ]

    return {
        "name": service.agent_name,
        "description": service.agent_description,
        "version": service.agent_version,
        "url": f"{base_url}/a2a",
        "defaultInputModes": ["text/plain"],
        "defaultOutputModes": ["text/plain"],
        "skills": skills,
        "capabilities": {"streaming": False},
    }


def create_a2a_handler(service: BaseAgentService) -> Any:
    """Build an A2A request handler from the agent service.

    Returns None until A2A is fully implemented (Phase 3.5).
    The transport layer checks for None and skips A2A route mounting.
    """
    logger.info("A2A handler not yet implemented (pending Phase 3.5 upgrade)")
    return None
