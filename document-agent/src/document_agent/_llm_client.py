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
            # Log stop reason and token usage
            stop = data.get("stop_reason")
            usage = data.get("usage", {})
            out_tokens = usage.get("output_tokens")
            if stop == "max_tokens":
                logger.warning(
                    "Response truncated (stop_reason=max_tokens, output_tokens=%s)",
                    out_tokens,
                )
            elif stop:
                logger.debug("stop_reason=%s, output_tokens=%s", stop, out_tokens)
            # Anthropic messages API
            for block in data.get("content", []):
                if block.get("type") == "text":
                    return block["text"]
            return ""
        # OpenAI / Azure / Mistral format
        choices = data.get("choices", [])
        if choices:
            finish = choices[0].get("finish_reason")
            if finish == "length":
                logger.warning("Response truncated (finish_reason=length)")
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
        # Scale timeout with max_tokens (large outputs need more time)
        timeout = max(120.0, max_tokens / 100)
        async with httpx.AsyncClient() as client:
            resp = await client.post(
                url,
                headers=headers,
                json=payload,
                timeout=timeout,
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

    async def chat_structured(
        self,
        messages: list[dict[str, Any]],
        schema: dict[str, Any],
        *,
        tool_name: str = "structured_output",
        image_b64: str | None = None,
        max_tokens: int = 4096,
    ) -> dict[str, Any]:
        """Chat with forced structured output matching a JSON schema.

        Uses tool_use (Anthropic) or response_format (OpenAI) to
        guarantee the response matches the schema.

        Returns the parsed dict.
        """
        import json

        if image_b64:
            messages = _inject_image(messages, image_b64, self.provider)

        url, headers, payload = self._build_request(messages, max_tokens=max_tokens)

        if self.provider in ("anthropic", "azure_anthropic"):
            # Anthropic: use tools + tool_choice to force structured output
            payload["tools"] = [
                {
                    "name": tool_name,
                    "description": "Return structured data",
                    "input_schema": schema,
                }
            ]
            payload["tool_choice"] = {"type": "tool", "name": tool_name}
        else:
            # OpenAI / Azure / Mistral: use response_format
            payload["response_format"] = {
                "type": "json_schema",
                "json_schema": {
                    "name": tool_name,
                    "strict": True,
                    "schema": schema,
                },
            }

        timeout = max(120.0, max_tokens / 100)
        async with httpx.AsyncClient() as client:
            resp = await client.post(url, headers=headers, json=payload, timeout=timeout)
            if resp.status_code >= 400:
                logger.error("LLM API error %d: %s", resp.status_code, resp.text[:500])
                resp.raise_for_status()

        data = resp.json()

        if self.provider in ("anthropic", "azure_anthropic"):
            # Extract tool_use input from response
            for block in data.get("content", []):
                if block.get("type") == "tool_use":
                    return block["input"]
            raise ValueError("No tool_use block in Anthropic response")
        else:
            # OpenAI: parse from message content
            text = self._extract_text(data)
            return json.loads(text)


def _inject_image(
    messages: list[dict[str, Any]],
    image_b64: str,
    provider: str,
) -> list[dict[str, Any]]:
    """Return a copy of messages with the image added to the
    last user message."""
    msgs = [m.copy() for m in messages]
    media_type, raw_b64 = _parse_image_b64(image_b64)
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
                            "media_type": media_type,
                            "data": raw_b64,
                        },
                    },
                ]
            else:
                # OpenAI / Azure / Mistral vision format
                data_uri = f"data:{media_type};base64,{raw_b64}"
                msgs[i]["content"] = [
                    {"type": "text", "text": text_part},
                    {
                        "type": "image_url",
                        "image_url": {"url": data_uri},
                    },
                ]
            break
    return msgs


def _parse_image_b64(b64: str) -> tuple[str, str]:
    """Extract media type and raw base64 from image data.

    Accepts:
    - data:image/png;base64,... (data URI)
    - data:image/jpeg,... (shorthand)
    - raw base64 (detects type from magic bytes)

    Returns (media_type, raw_base64).
    """
    import base64 as b64mod

    if b64.startswith("data:"):
        # Parse data URI
        header, _, raw = b64.partition(",")
        # header = "data:image/png;base64" or "data:image/jpeg"
        mime = header.split(";")[0].removeprefix("data:")
        return mime, raw

    # Raw base64 - sniff magic bytes
    try:
        header_bytes = b64mod.b64decode(b64[:32])
    except Exception:
        return "image/png", b64

    if header_bytes[:3] == b"\xff\xd8\xff":
        return "image/jpeg", b64
    if header_bytes[:4] == b"\x89PNG":
        return "image/png", b64
    if header_bytes[:4] == b"RIFF" and header_bytes[8:12] == b"WEBP":
        return "image/webp", b64
    if header_bytes[:3] == b"GIF":
        return "image/gif", b64
    return "image/png", b64
