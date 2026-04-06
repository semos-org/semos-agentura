"""Generic async LLM chat client supporting multiple providers."""

from __future__ import annotations

import logging
from typing import Any

import httpx

logger = logging.getLogger(__name__)


def _detect_provider(endpoint: str) -> str:
    """Auto-detect API provider from endpoint URL."""
    e = endpoint.lower()
    # Azure-hosted Anthropic models (e.g. .../anthropic)
    if "services.ai.azure.com" in e and "/anthropic" in e:
        return "azure_anthropic"
    if "services.ai.azure.com" in e or "openai.azure.com" in e:
        return "azure"
    if "api.mistral.ai" in e:
        return "mistral"
    if "api.anthropic.com" in e:
        return "anthropic"
    if "api.openai.com" in e:
        return "openai"
    return "openai"  # default to OpenAI-compatible


class LLMClient:
    """Thin async wrapper for chat completions."""

    def __init__(
        self,
        endpoint: str,
        api_key: str,
        model: str,
    ):
        self.endpoint = endpoint.rstrip("/")
        self.api_key = api_key
        self.model = model
        self.provider = _detect_provider(endpoint)

    def _build_request(
        self,
        messages: list[dict[str, Any]],
        *,
        max_tokens: int = 4096,
    ) -> tuple[str, dict[str, str], dict[str, Any]]:
        """Build (url, headers, payload) for the provider."""
        if self.provider in ("azure_anthropic", "anthropic"):
            # Extract system messages (Anthropic uses top-level
            # 'system' param, not role: system in messages)
            system_parts = []
            non_system = []
            for m in messages:
                if m.get("role") == "system":
                    system_parts.append(m["content"])
                else:
                    non_system.append(m)

            url = f"{self.endpoint}/v1/messages"
            if self.provider == "azure_anthropic":
                headers = {
                    "Content-Type": "application/json",
                    "Authorization": f"Bearer {self.api_key}",
                    "anthropic-version": "2023-06-01",
                }
            else:
                headers = {
                    "Content-Type": "application/json",
                    "x-api-key": self.api_key,
                    "anthropic-version": "2023-06-01",
                }
            payload: dict[str, Any] = {
                "model": self.model,
                "max_tokens": max_tokens,
                "messages": non_system,
            }
            if system_parts:
                payload["system"] = "\n\n".join(system_parts)
        elif self.provider == "mistral":
            url = f"{self.endpoint}/v1/chat/completions"
            headers = {
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self.api_key}",
            }
            payload = {
                "model": self.model,
                "max_tokens": max_tokens,
                "messages": messages,
            }
        else:
            # Azure AI Foundry / OpenAI / OpenAI-compatible
            url = f"{self.endpoint}/chat/completions"
            headers = {
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self.api_key}",
            }
            payload = {
                "model": self.model,
                "max_tokens": max_tokens,
                "messages": messages,
            }
        return url, headers, payload

    def _extract_text(self, data: dict[str, Any]) -> str:
        """Extract assistant text from provider response."""
        if self.provider in ("anthropic", "azure_anthropic"):
            # Anthropic messages API
            for block in data.get("content", []):
                if block.get("type") == "text":
                    return block["text"]
            return ""
        # OpenAI / Azure / Mistral format
        choices = data.get("choices", [])
        if choices:
            return choices[0]["message"]["content"]
        return ""

    async def chat(
        self,
        messages: list[dict[str, Any]],
        *,
        max_tokens: int = 4096,
    ) -> str:
        """Send chat completion, return assistant text."""
        url, headers, payload = self._build_request(
            messages,
            max_tokens=max_tokens,
        )
        async with httpx.AsyncClient() as client:
            resp = await client.post(
                url,
                headers=headers,
                json=payload,
                timeout=120.0,
            )
            if resp.status_code >= 400:
                logger.error(
                    "LLM API error %d: %s",
                    resp.status_code,
                    resp.text[:500],
                )
                resp.raise_for_status()
            return self._extract_text(resp.json())

    async def chat_with_image(
        self,
        messages: list[dict[str, Any]],
        image_b64: str,
        *,
        max_tokens: int = 4096,
    ) -> str:
        """Send chat with an inline base64 image for vision.

        Appends the image to the last user message as a
        multi-part content block.
        """
        msgs = _inject_image(messages, image_b64, self.provider)
        return await self.chat(msgs, max_tokens=max_tokens)


def _inject_image(
    messages: list[dict[str, Any]],
    image_b64: str,
    provider: str,
) -> list[dict[str, Any]]:
    """Return a copy of messages with the image added to the
    last user message."""
    msgs = [m.copy() for m in messages]
    # Find last user message
    for i in range(len(msgs) - 1, -1, -1):
        if msgs[i].get("role") == "user":
            content = msgs[i].get("content", "")
            if isinstance(content, str):
                text_part = content
            else:
                text_part = content
                break

            if provider in ("anthropic", "azure_anthropic"):
                msgs[i]["content"] = [
                    {"type": "text", "text": text_part},
                    {
                        "type": "image",
                        "source": {
                            "type": "base64",
                            "media_type": "image/png",
                            "data": _strip_data_uri(image_b64),
                        },
                    },
                ]
            else:
                # OpenAI / Azure / Mistral vision format
                data_uri = image_b64
                if not data_uri.startswith("data:"):
                    data_uri = f"data:image/png;base64,{data_uri}"
                msgs[i]["content"] = [
                    {"type": "text", "text": text_part},
                    {
                        "type": "image_url",
                        "image_url": {"url": data_uri},
                    },
                ]
            break
    return msgs


def _strip_data_uri(b64: str) -> str:
    """Strip 'data:image/...;base64,' prefix if present."""
    if b64.startswith("data:"):
        _, _, raw = b64.partition(",")
        return raw
    return b64
