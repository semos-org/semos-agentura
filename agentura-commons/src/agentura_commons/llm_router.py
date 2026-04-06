"""LLM-based tool routing for A2A natural language requests.

Given a user message and a list of tool definitions, asks an LLM
to select the right tool and extract arguments. Uses direct HTTP
calls to support Azure AI Foundry Anthropic endpoints (which need
Bearer auth + anthropic-version header).
"""

from __future__ import annotations

import inspect
import json
import logging
from typing import TYPE_CHECKING, Any

import httpx

if TYPE_CHECKING:
    from .base import ToolDef

logger = logging.getLogger(__name__)

_SYSTEM = (
    "You are a tool router. Given the user message, select "
    "exactly one tool and provide its arguments. "
    "You MUST use one of the provided tools."
)


def _detect_provider(endpoint: str) -> str:
    """Auto-detect API provider from endpoint URL."""
    e = endpoint.lower()
    if "services.ai.azure.com" in e and "/anthropic" in e:
        return "azure_anthropic"
    if "api.anthropic.com" in e:
        return "anthropic"
    return "openai"


def _tool_schema(t: ToolDef) -> dict[str, Any]:
    """Convert a ToolDef to a tool schema."""
    if t.parameters:
        params = t.parameters
    else:
        sig = inspect.signature(t.fn)
        props = {}
        required = []
        for name, p in sig.parameters.items():
            if name == "self":
                continue
            prop: dict[str, Any] = {"type": "string"}
            ann = p.annotation
            if ann is int:
                prop["type"] = "integer"
            elif ann is float:
                prop["type"] = "number"
            elif ann is bool:
                prop["type"] = "boolean"
            if t.description:
                prop["description"] = ""
            props[name] = prop
            if p.default is inspect.Parameter.empty:
                required.append(name)
        params = {
            "type": "object",
            "properties": props,
            "required": required,
        }
    return {
        "name": t.name,
        "description": t.description,
        "input_schema": params,
    }


def _tool_schema_openai(t: ToolDef) -> dict[str, Any]:
    """OpenAI-format tool schema."""
    schema = _tool_schema(t)
    return {
        "type": "function",
        "function": {
            "name": schema["name"],
            "description": schema["description"],
            "parameters": schema["input_schema"],
        },
    }


async def route_to_tool(
    message: str,
    tools: list[ToolDef],
    *,
    model: str,
    api_key: str = "",
    api_base: str = "",
) -> tuple[str, dict[str, Any]] | None:
    """Use an LLM to pick a tool and extract arguments.

    Returns (tool_name, arguments) or None if routing fails.
    Supports Azure AI Foundry Anthropic, Anthropic, and
    OpenAI-compatible endpoints.
    """
    if not model or not api_base:
        logger.warning("No router LLM configured")
        return None

    provider = _detect_provider(api_base)

    try:
        if provider in ("azure_anthropic", "anthropic"):
            return await _route_anthropic(
                message, tools, model=model,
                api_key=api_key, api_base=api_base,
                provider=provider,
            )
        else:
            return await _route_openai(
                message, tools, model=model,
                api_key=api_key, api_base=api_base,
            )
    except Exception:
        logger.exception("LLM routing failed")
        return None


async def _route_anthropic(
    message: str,
    tools: list[ToolDef],
    *,
    model: str,
    api_key: str,
    api_base: str,
    provider: str,
) -> tuple[str, dict[str, Any]] | None:
    """Route via Anthropic Messages API (native or Azure)."""
    url = f"{api_base.rstrip('/')}/v1/messages"
    headers: dict[str, str] = {
        "Content-Type": "application/json",
        "anthropic-version": "2023-06-01",
    }
    if provider == "azure_anthropic":
        headers["Authorization"] = f"Bearer {api_key}"
    else:
        headers["x-api-key"] = api_key

    payload = {
        "model": model,
        "max_tokens": 1024,
        "system": _SYSTEM,
        "tools": [_tool_schema(t) for t in tools],
        "tool_choice": {"type": "any"},
        "messages": [{"role": "user", "content": message}],
    }

    async with httpx.AsyncClient() as client:
        resp = await client.post(
            url, headers=headers, json=payload, timeout=30.0,
        )
        if resp.status_code >= 400:
            logger.error(
                "Router LLM error %d: %s",
                resp.status_code, resp.text[:300],
            )
            return None
        data = resp.json()

    # Anthropic returns tool_use blocks in content
    for block in data.get("content", []):
        if block.get("type") == "tool_use":
            name = block["name"]
            args = block.get("input", {})
            logger.info("LLM routed to tool=%s", name)
            return name, args

    return None


async def _route_openai(
    message: str,
    tools: list[ToolDef],
    *,
    model: str,
    api_key: str,
    api_base: str,
) -> tuple[str, dict[str, Any]] | None:
    """Route via OpenAI-compatible API."""
    url = f"{api_base.rstrip('/')}/chat/completions"
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {api_key}",
    }
    payload = {
        "model": model,
        "max_tokens": 1024,
        "tools": [_tool_schema_openai(t) for t in tools],
        "tool_choice": "required",
        "messages": [
            {"role": "system", "content": _SYSTEM},
            {"role": "user", "content": message},
        ],
    }

    async with httpx.AsyncClient() as client:
        resp = await client.post(
            url, headers=headers, json=payload, timeout=30.0,
        )
        if resp.status_code >= 400:
            logger.error(
                "Router LLM error %d: %s",
                resp.status_code, resp.text[:300],
            )
            return None
        data = resp.json()

    choices = data.get("choices", [])
    if choices:
        tc = choices[0].get("message", {}).get("tool_calls", [])
        if tc:
            call = tc[0]["function"]
            name = call["name"]
            args = json.loads(call["arguments"])
            logger.info("LLM routed to tool=%s", name)
            return name, args

    return None
