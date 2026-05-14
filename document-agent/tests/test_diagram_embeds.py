"""Tests for embed parameter threading in diagram generation."""

from __future__ import annotations

import io
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from document_agent.composition._diagram_optimize import _build_initial_messages
from PIL import Image


def _make_png(tmp_path: Path, name: str = "icon.png", w: int = 48, h: int = 48) -> Path:
    img = Image.new("RGBA", (w, h), (255, 0, 0, 255))
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    p = tmp_path / name
    p.write_bytes(buf.getvalue())
    return p


class TestBuildInitialMessagesWithEmbeds:
    def test_embed_descriptions_in_system_prompt(self):
        embed_lines = [
            "__IMG_1__ = cloud.png (cloud icon, 48x48px)",
            "__IMG_2__ = db.png (db icon, 64x64px)",
        ]
        messages = _build_initial_messages(
            system="You are a diagram expert.",
            description="Create a network diagram",
            source=None,
            diagram_type="drawio",
            embed_descriptions=embed_lines,
        )
        system_msg = messages[0]["content"]
        assert "__IMG_1__ = cloud.png (cloud icon, 48x48px)" in system_msg
        assert "__IMG_2__ = db.png (db icon, 64x64px)" in system_msg
        assert "shape=image;image=__IMG_" in system_msg

    def test_no_embeds_no_change(self):
        messages = _build_initial_messages(
            system="You are a diagram expert.",
            description="Create a flowchart",
            source=None,
            diagram_type="drawio",
            embed_descriptions=None,
        )
        system_msg = messages[0]["content"]
        assert "Available images to embed" not in system_msg


class TestAutoSwitchToDrawio:
    @pytest.mark.asyncio
    async def test_embeds_force_drawio(self, tmp_path):
        from document_agent.composition._generate_diagram import generate_diagram
        from document_agent.config import Settings

        icon = _make_png(tmp_path, "icon.png")

        mock_result = MagicMock()
        mock_result.code = "<mxfile></mxfile>"
        mock_result.image_path = None
        mock_result.iterations = 1
        mock_result.review_log = []

        mock_optimize = AsyncMock(return_value=mock_result)

        settings = Settings(
            _env_file=None,
            diagram_codegen_endpoint="http://x",
            diagram_codegen_api_key="k",
            diagram_codegen_model="m",
        )

        with (
            patch(
                "document_agent.composition._generate_diagram.optimize_diagram",
                mock_optimize,
            ),
            patch(
                "document_agent.composition._generate_diagram.require_tool",
                return_value="drawio",
            ),
        ):
            await generate_diagram(
                description="network diagram",
                diagram_type="mermaid",
                embeds=[{"path": icon, "description": "server icon"}],
                output_dir=tmp_path,
                settings=settings,
            )

        # Should have been called with drawio, not mermaid
        call_args = mock_optimize.call_args
        assert call_args[0][1] == "drawio"


class TestEmbedPlacementInXml:
    """Test that embeds are correctly placed and restored in draw.io XML."""

    def test_embed_roundtrip_in_xml(self, tmp_path):
        """Simulate the full flow: prepare embeds, LLM places them, restore."""
        from document_agent.composition._diagram_source import (
            prepare_embed_images,
            restore_embedded_images,
        )

        icon = _make_png(tmp_path, "cloud.png", 64, 64)
        store, prompts = prepare_embed_images(
            files=[icon],
            descriptions=["cloud icon"],
        )

        # Simulate LLM output that uses the placeholder
        llm_xml = (
            '<mxfile><diagram name="P"><mxGraphModel><root>'
            '<mxCell id="0"/>'
            '<mxCell id="1" parent="0"/>'
            '<mxCell id="2" value="Server" style="rounded=1;" vertex="1" parent="1">'
            '<mxGeometry x="10" y="10" width="120" height="60" as="geometry"/>'
            "</mxCell>"
            '<mxCell id="3" style="shape=image;image=__IMG_1__;" vertex="1" parent="1">'
            '<mxGeometry x="200" y="10" width="64" height="64" as="geometry"/>'
            "</mxCell>"
            "</root></mxGraphModel></diagram></mxfile>"
        )

        restored = restore_embedded_images(llm_xml, store)

        # Placeholder replaced
        assert "__IMG_1__" not in restored
        # Data URI present (draw.io shorthand, no ";base64,")
        assert "data:image/png," in restored
        # Original non-image cells preserved
        assert "Server" in restored
        # Valid XML
        from lxml import etree

        root = etree.fromstring(restored.encode("utf-8"))
        img_cells = [c for c in root.iter("mxCell") if "shape=image" in (c.get("style") or "")]
        assert len(img_cells) == 1
        style = img_cells[0].get("style")
        # Verify the data URI is in the style and has no semicolons
        # that would break draw.io style parsing
        assert "image=data:image/png," in style
