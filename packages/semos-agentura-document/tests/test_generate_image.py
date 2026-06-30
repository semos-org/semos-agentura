"""Tests for standalone image generation."""

from __future__ import annotations

import io
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from PIL import Image
from semos.agentura.document.config import Settings


def _make_png_bytes(w: int = 64, h: int = 64) -> bytes:
    img = Image.new("RGBA", (w, h), (255, 0, 0, 255))
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


class TestGenerateMode:
    @pytest.mark.asyncio
    async def test_generate_writes_png(self, tmp_path):
        from semos.agentura.document.composition._generate_image import generate_image

        fake_png = _make_png_bytes()
        mock_client = AsyncMock()
        mock_client.generate.return_value = fake_png

        with patch(
            "semos.agentura.document.composition._generate_image._build_image_client",
            return_value=mock_client,
        ):
            result = await generate_image(
                description="a red square icon",
                mode="generate",
                output_dir=tmp_path,
                settings=Settings(_env_file=None, image_gen_endpoint="http://x", image_gen_api_key="k"),
            )

        assert result.image_path.exists()
        assert result.mode == "generate"
        assert result.size == (64, 64)
        mock_client.generate.assert_called_once()

    @pytest.mark.asyncio
    async def test_generate_prepends_style(self, tmp_path):
        from semos.agentura.document.composition._generate_image import generate_image

        fake_png = _make_png_bytes()
        mock_client = AsyncMock()
        mock_client.generate.return_value = fake_png

        with patch(
            "semos.agentura.document.composition._generate_image._build_image_client",
            return_value=mock_client,
        ):
            await generate_image(
                description="a server",
                mode="generate",
                style="flat icon",
                output_dir=tmp_path,
                settings=Settings(_env_file=None, image_gen_endpoint="http://x", image_gen_api_key="k"),
            )

        call_args = mock_client.generate.call_args
        prompt = call_args.args[0] if call_args.args else call_args.kwargs.get("prompt", "")
        assert "flat icon" in prompt


class TestEditMode:
    @pytest.mark.asyncio
    async def test_edit_calls_client_edit(self, tmp_path):
        from semos.agentura.document.composition._generate_image import generate_image

        source_png = _make_png_bytes()
        source_path = tmp_path / "source.png"
        source_path.write_bytes(source_png)

        edited_png = _make_png_bytes(32, 32)
        mock_client = AsyncMock()
        mock_client.edit.return_value = edited_png

        with patch(
            "semos.agentura.document.composition._generate_image._build_image_client",
            return_value=mock_client,
        ):
            result = await generate_image(
                description="make it blue",
                mode="edit",
                source=source_path,
                output_dir=tmp_path,
                settings=Settings(_env_file=None, image_gen_endpoint="http://x", image_gen_api_key="k"),
            )

        assert result.mode == "edit"
        mock_client.edit.assert_called_once()


class TestCutMode:
    @pytest.mark.asyncio
    async def test_cut_vlm_guided(self, tmp_path):
        from semos.agentura.document.composition._generate_image import generate_image

        source_png = _make_png_bytes(200, 200)
        source_path = tmp_path / "source.png"
        source_path.write_bytes(source_png)

        mock_image_client = AsyncMock()
        mock_vlm_client = MagicMock()
        mock_vlm_client.chat_structured = AsyncMock(
            return_value={
                "elements": [
                    {"label": "red square", "x": 50, "y": 50, "width": 60, "height": 60},
                ]
            },
        )

        with (
            patch(
                "semos.agentura.document.composition._generate_image._build_image_client",
                return_value=mock_image_client,
            ),
            patch(
                "semos.agentura.document.composition._generate_image._build_vlm_client",
                return_value=mock_vlm_client,
            ),
        ):
            result = await generate_image(
                description="the red square",
                mode="cut",
                source=source_path,
                output_dir=tmp_path,
                settings=Settings(_env_file=None, image_gen_endpoint="http://x", image_gen_api_key="k"),
            )

        assert result.mode == "cut"
        assert result.image_path.exists()
        mock_vlm_client.chat_structured.assert_called_once()

    @pytest.mark.asyncio
    async def test_cut_fallback_to_image_edit(self, tmp_path):
        from semos.agentura.document.composition._generate_image import generate_image

        source_png = _make_png_bytes(100, 100)
        source_path = tmp_path / "source.png"
        source_path.write_bytes(source_png)

        edited_png = _make_png_bytes(50, 50)
        mock_image_client = AsyncMock()
        mock_image_client.edit.return_value = edited_png
        mock_vlm_client = MagicMock()
        mock_vlm_client.chat_structured = AsyncMock(
            side_effect=Exception("VLM failed"),
        )

        with (
            patch(
                "semos.agentura.document.composition._generate_image._build_image_client",
                return_value=mock_image_client,
            ),
            patch(
                "semos.agentura.document.composition._generate_image._build_vlm_client",
                return_value=mock_vlm_client,
            ),
        ):
            result = await generate_image(
                description="the icon",
                mode="cut",
                source=source_path,
                output_dir=tmp_path,
                settings=Settings(_env_file=None, image_gen_endpoint="http://x", image_gen_api_key="k"),
            )

        assert result.mode == "cut"
        mock_image_client.edit.assert_called_once()


class TestMissingConfig:
    @pytest.mark.asyncio
    async def test_raises_without_endpoint(self, tmp_path):
        from semos.agentura.document.composition._generate_image import generate_image
        from semos.agentura.document.exceptions import ProviderError

        settings = Settings(
            _env_file=None,
            image_gen_endpoint=None,
            image_gen_api_key=None,
        )

        with pytest.raises(ProviderError, match="Image generation not configured"):
            await generate_image(
                description="anything",
                output_dir=tmp_path,
                settings=settings,
            )


# Integration tests - require real IMAGE_GEN_ENDPOINT


def _load_env():
    import os

    from dotenv import load_dotenv

    agent_dir = Path(__file__).resolve().parent.parent
    load_dotenv(agent_dir / ".env")
    return os.environ


def _has_image_gen() -> bool:
    return bool(_load_env().get("IMAGE_GEN_ENDPOINT"))


_skip_no_image_gen = pytest.mark.skipif(
    not _has_image_gen(),
    reason="No IMAGE_GEN_ENDPOINT configured",
)


@pytest.mark.integration
@_skip_no_image_gen
class TestGenerateImageIntegration:
    @pytest.mark.asyncio
    async def test_generate_with_style(self, tmp_path):
        from semos.agentura.document.composition._generate_image import generate_image

        result = await generate_image(
            description="a server rack",
            mode="generate",
            style="isometric flat icon",
            size="1024x1024",
            output_dir=tmp_path,
        )
        assert result.image_path.exists()
        assert result.image_path.stat().st_size > 1000
        assert result.mode == "generate"
        assert "isometric flat icon" in result.prompt
        img = Image.open(result.image_path)
        assert img.size[0] > 0

    @pytest.mark.asyncio
    async def test_edit_modifies_image(self, tmp_path):
        from semos.agentura.document.composition._generate_image import generate_image

        source_img = Image.new("RGB", (1024, 1024), (255, 0, 0))
        source_path = tmp_path / "red.png"
        buf = io.BytesIO()
        source_img.save(buf, format="PNG")
        source_path.write_bytes(buf.getvalue())

        result = await generate_image(
            description="Change the color to blue",
            mode="edit",
            source=source_path,
            output_dir=tmp_path,
        )
        assert result.image_path.exists()
        assert result.mode == "edit"
        assert result.image_path.stat().st_size > 100
