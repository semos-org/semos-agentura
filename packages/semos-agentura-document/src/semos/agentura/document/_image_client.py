"""Async wrapper for image generation APIs.

Supports three provider types:
- openai: OpenAI or Azure OpenAI (images/generations endpoint, size param)
- foundry: Azure AI Foundry providers or direct provider APIs
  (v1/{model} endpoint, width/height params)
"""

from __future__ import annotations

import asyncio
import base64
import logging

import httpx

logger = logging.getLogger(__name__)

_MAX_RETRIES = 3
_RETRY_BACKOFF = (10, 30, 60)  # seconds between retries

_AZURE_OPENAI_API_VERSION = "2025-04-01-preview"


def _detect_provider(endpoint: str) -> str:
    """Auto-detect image API provider from endpoint URL.

    Returns:
        "azure_openai" - Azure OpenAI (cognitiveservices.azure.com)
            URL: {endpoint}/images/generations?api-version=...
            Auth: api-key header
            Params: size="WxH", output_format
        "openai" - OpenAI (api.openai.com)
            URL: {endpoint}/images/generations
            Auth: Bearer token
            Params: size="WxH", model, response_format
        "foundry" - Azure AI Foundry providers or direct provider APIs
            URL: {endpoint}/v1/{model}
            Auth: Bearer token
            Params: width, height, model, n
    """
    e = endpoint.lower()
    if "cognitiveservices.azure.com" in e or "openai.azure.com" in e:
        return "azure_openai"
    if "api.openai.com" in e:
        return "openai"
    # Azure AI Foundry providers or direct provider APIs (BFL, etc.)
    return "foundry"


def _parse_size(size: str) -> tuple[int, int]:
    """Parse "WxH" string to (width, height) tuple."""
    w, _, h = size.partition("x")
    return int(w), int(h)


class ImageClient:
    """Thin async wrapper for image generation/editing APIs.

    Provider types and endpoint configuration:

    azure_openai (cognitiveservices.azure.com):
      IMAGE_GEN_ENDPOINT = https://{resource}.cognitiveservices.azure.com/openai/deployments/{model}

    openai (api.openai.com):
      IMAGE_GEN_ENDPOINT = https://api.openai.com

    foundry (Azure AI Foundry providers or direct APIs):
      IMAGE_GEN_ENDPOINT = https://{resource}.services.ai.azure.com/providers/blackforestlabs
      IMAGE_GEN_ENDPOINT = https://api.bfl.ai
    """

    def __init__(self, endpoint: str, api_key: str, model: str):
        self.endpoint = endpoint.rstrip("/")
        self.api_key = api_key
        self.model = model
        self.provider = _detect_provider(endpoint)

    def _build_gen_url(self) -> str:
        """Build the image generation URL for this provider."""
        if self.provider == "azure_openai":
            return f"{self.endpoint}/images/generations?api-version={_AZURE_OPENAI_API_VERSION}"
        if self.provider == "openai":
            return f"{self.endpoint}/images/generations"
        # foundry: {base}/v1/{model}
        return f"{self.endpoint}/v1/{self.model}"

    def _build_edit_url(self) -> str:
        """Build the image edit URL for this provider."""
        if self.provider == "azure_openai":
            return f"{self.endpoint}/images/edits?api-version={_AZURE_OPENAI_API_VERSION}"
        # OpenAI and foundry
        return f"{self.endpoint}/images/edits"

    def _build_headers(self, *, content_type: str | None = "application/json") -> dict[str, str]:
        """Build request headers for the provider."""
        headers: dict[str, str] = {}
        if content_type:
            headers["Content-Type"] = content_type
        if self.provider == "azure_openai":
            headers["api-key"] = self.api_key
        else:
            headers["Authorization"] = f"Bearer {self.api_key}"
        return headers

    async def generate(
        self,
        prompt: str,
        *,
        size: str = "1024x1024",
        background: str = "auto",
        output_format: str = "png",
    ) -> bytes:
        """Generate image from text prompt. Returns raw image bytes."""
        url = self._build_gen_url()
        headers = self._build_headers()

        if self.provider == "foundry":
            w, h = _parse_size(size)
            payload: dict = {
                "prompt": prompt,
                "width": w,
                "height": h,
                "n": 1,
                "model": self.model,
            }
        elif self.provider == "azure_openai":
            payload = {
                "prompt": prompt,
                "size": size,
                "output_format": output_format,
            }
        else:
            # openai
            payload = {
                "prompt": prompt,
                "size": size,
                "model": self.model,
                "response_format": "b64_json",
            }
            if output_format != "png":
                payload["output_format"] = output_format
            if background != "auto":
                payload["background"] = background

        return await self._post_with_retry(url, headers=headers, json=payload)

    async def edit(
        self,
        image: bytes,
        prompt: str,
        *,
        mask: bytes | None = None,
        size: str = "1024x1024",
    ) -> bytes:
        """Edit existing image with text prompt. Returns raw image bytes.

        For foundry providers (Flux): sends input_image as base64 in JSON
        body to the same generation endpoint. No mask support.
        For OpenAI providers: multipart POST to /images/edits with optional mask.
        """
        if self.provider == "foundry":
            return await self._edit_foundry(image, prompt, size=size)
        url = self._build_edit_url()
        headers = self._build_headers(content_type=None)
        files: dict = {
            "image": ("image.png", image, "image/png"),
        }
        if mask:
            files["mask"] = ("mask.png", mask, "image/png")
        form_data: dict[str, str] = {
            "prompt": prompt,
            "size": size,
        }
        if self.provider != "azure_openai":
            form_data["model"] = self.model
            form_data["response_format"] = "b64_json"
        return await self._post_with_retry(url, headers=headers, files=files, data=form_data)

    async def _edit_foundry(
        self,
        image: bytes,
        prompt: str,
        *,
        size: str = "1024x1024",
    ) -> bytes:
        """Edit via foundry provider (Flux): same endpoint, add input_image."""
        url = self._build_gen_url()
        headers = self._build_headers()
        w, h = _parse_size(size)
        payload: dict = {
            "prompt": prompt,
            "input_image": base64.b64encode(image).decode(),
            "width": w,
            "height": h,
            "n": 1,
            "model": self.model,
        }
        return await self._post_with_retry(url, headers=headers, json=payload)

    async def _post_with_retry(self, url: str, **kwargs) -> bytes:
        """POST with retry on 429/5xx. Returns decoded image bytes."""
        last_exc: Exception | None = None
        for attempt in range(_MAX_RETRIES):
            async with httpx.AsyncClient() as client:
                resp = await client.post(url, timeout=300.0, **kwargs)
            if resp.status_code == 429 or resp.status_code >= 500:
                wait = _RETRY_BACKOFF[min(attempt, len(_RETRY_BACKOFF) - 1)]
                logger.warning(
                    "Image API %d (attempt %d/%d), retrying in %ds: %s",
                    resp.status_code,
                    attempt + 1,
                    _MAX_RETRIES,
                    wait,
                    resp.text[:200],
                )
                last_exc = httpx.HTTPStatusError(
                    f"{resp.status_code}",
                    request=resp.request,
                    response=resp,
                )
                await asyncio.sleep(wait)
                continue
            if resp.status_code >= 400:
                logger.error("Image API error %d: %s", resp.status_code, resp.text[:500])
                resp.raise_for_status()
            data = resp.json()
            b64 = data["data"][0]["b64_json"]
            return base64.b64decode(b64)
        # All retries exhausted
        raise last_exc or RuntimeError("Image API request failed after retries")
