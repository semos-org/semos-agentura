"""Tests for ImageClient (image generation API wrapper)."""

from __future__ import annotations

import base64
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from document_agent._image_client import ImageClient, _detect_provider

# Provider detection


class TestDetectProvider:
    def test_azure_openai(self):
        assert _detect_provider("https://x.cognitiveservices.azure.com/openai/deployments/m") == "azure_openai"

    def test_openai(self):
        assert _detect_provider("https://api.openai.com") == "openai"

    def test_foundry_azure(self):
        assert _detect_provider("https://x.services.ai.azure.com/providers/blackforestlabs") == "foundry"

    def test_foundry_direct(self):
        assert _detect_provider("https://api.bfl.ai") == "foundry"


# Fixtures


@pytest.fixture
def openai_client():
    return ImageClient(
        endpoint="https://api.openai.com",
        api_key="test-key",
        model="gpt-image-2",
    )


@pytest.fixture
def foundry_client():
    return ImageClient(
        endpoint="https://example.services.ai.azure.com/providers/blackforestlabs",
        api_key="test-key",
        model="flux-2-pro",
    )


def _mock_http(b64_response: str):
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = {"data": [{"b64_json": b64_response}]}
    mock_client = MagicMock()
    mock_client.post = AsyncMock(return_value=mock_resp)
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=None)
    return mock_client


# OpenAI provider tests


class TestOpenAIGenerate:
    @pytest.mark.asyncio
    async def test_generate_returns_bytes(self, openai_client):
        fake_b64 = base64.b64encode(b"fake-png-data").decode()
        mock = _mock_http(fake_b64)

        with patch("httpx.AsyncClient", return_value=mock):
            result = await openai_client.generate("a cloud icon")

        assert result == b"fake-png-data"
        payload = mock.post.call_args.kwargs.get("json")
        assert payload["model"] == "gpt-image-2"
        assert payload["prompt"] == "a cloud icon"
        assert payload["response_format"] == "b64_json"
        assert payload["size"] == "1024x1024"

    @pytest.mark.asyncio
    async def test_generate_background_param(self, openai_client):
        mock = _mock_http(base64.b64encode(b"x").decode())

        with patch("httpx.AsyncClient", return_value=mock):
            await openai_client.generate("icon", background="transparent")

        payload = mock.post.call_args.kwargs.get("json")
        assert payload["background"] == "transparent"

    @pytest.mark.asyncio
    async def test_generate_url(self, openai_client):
        mock = _mock_http(base64.b64encode(b"x").decode())

        with patch("httpx.AsyncClient", return_value=mock):
            await openai_client.generate("icon")

        url = mock.post.call_args.args[0]
        assert url == "https://api.openai.com/images/generations"


class TestOpenAIEdit:
    @pytest.mark.asyncio
    async def test_edit_returns_bytes(self, openai_client):
        fake_b64 = base64.b64encode(b"edited").decode()
        mock = _mock_http(fake_b64)

        with patch("httpx.AsyncClient", return_value=mock):
            result = await openai_client.edit(image=b"src", prompt="blue")

        assert result == b"edited"

    @pytest.mark.asyncio
    async def test_edit_with_mask(self, openai_client):
        mock = _mock_http(base64.b64encode(b"x").decode())

        with patch("httpx.AsyncClient", return_value=mock):
            await openai_client.edit(image=b"src", prompt="fill", mask=b"mask")

        files = mock.post.call_args.kwargs.get("files", {})
        assert any("mask" in str(k) for k in files)


# Foundry provider tests


class TestFoundryGenerate:
    @pytest.mark.asyncio
    async def test_generate_uses_width_height(self, foundry_client):
        mock = _mock_http(base64.b64encode(b"flux-img").decode())

        with patch("httpx.AsyncClient", return_value=mock):
            result = await foundry_client.generate("a fox", size="256x256")

        assert result == b"flux-img"
        payload = mock.post.call_args.kwargs.get("json")
        assert payload["width"] == 256
        assert payload["height"] == 256
        assert payload["model"] == "flux-2-pro"
        assert payload["n"] == 1
        assert "size" not in payload
        assert "response_format" not in payload

    @pytest.mark.asyncio
    async def test_generate_url_pattern(self, foundry_client):
        mock = _mock_http(base64.b64encode(b"x").decode())

        with patch("httpx.AsyncClient", return_value=mock):
            await foundry_client.generate("icon")

        url = mock.post.call_args.args[0]
        assert url == "https://example.services.ai.azure.com/providers/blackforestlabs/v1/flux-2-pro"


class TestFoundryEdit:
    @pytest.mark.asyncio
    async def test_edit_sends_input_image_in_json(self, foundry_client):
        mock = _mock_http(base64.b64encode(b"edited-flux").decode())

        with patch("httpx.AsyncClient", return_value=mock):
            result = await foundry_client.edit(image=b"src-img", prompt="make blue", size="256x256")

        assert result == b"edited-flux"
        payload = mock.post.call_args.kwargs.get("json")
        # Foundry edit uses JSON body with input_image, not multipart
        assert payload["input_image"] == base64.b64encode(b"src-img").decode()
        assert payload["prompt"] == "make blue"
        assert payload["width"] == 256
        assert payload["height"] == 256
        assert payload["model"] == "flux-2-pro"
        # Same generation URL
        url = mock.post.call_args.args[0]
        assert "/v1/flux-2-pro" in url


# Error handling


class TestErrorHandling:
    @pytest.mark.asyncio
    async def test_raises_on_400(self, openai_client):
        mock_resp = MagicMock()
        mock_resp.status_code = 400
        mock_resp.text = "Bad request"
        mock_resp.raise_for_status.side_effect = Exception("400 Bad Request")
        mock = MagicMock()
        mock.post = AsyncMock(return_value=mock_resp)
        mock.__aenter__ = AsyncMock(return_value=mock)
        mock.__aexit__ = AsyncMock(return_value=None)

        with patch("httpx.AsyncClient", return_value=mock):
            with pytest.raises(Exception, match="400"):
                await openai_client.generate("bad prompt")
