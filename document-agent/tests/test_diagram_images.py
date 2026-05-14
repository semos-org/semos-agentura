"""Tests for draw.io embedded image stripping, restoration, and embed pipeline."""

from __future__ import annotations

import io
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from document_agent.composition._diagram_optimize import _build_initial_messages
from document_agent.composition._diagram_source import (
    _decompress_and_strip_drawio,
    prepare_embed_images,
    restore_embedded_images,
    strip_embedded_images,
)
from PIL import Image


def _make_embed_png(tmp_path: Path, name: str = "icon.png", w: int = 48, h: int = 48) -> Path:
    img = Image.new("RGBA", (w, h), (255, 0, 0, 255))
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    p = tmp_path / name
    p.write_bytes(buf.getvalue())
    return p


class TestStripEmbeddedImages:
    def test_strips_drawio_shorthand_format(self):
        b64 = "A" * 200
        xml = (
            f'<mxfile><diagram name="P"><mxGraphModel><root>'
            f'<mxCell id="0"/>'
            f'<mxCell id="img1" style="shape=image;image=data:image/png,{b64}"/>'
            f"</root></mxGraphModel></diagram></mxfile>"
        )
        stripped, store = strip_embedded_images(xml)
        assert store.get("cells")
        assert store.get("uris")
        assert b64 not in stripped
        assert "__IMG_1__" in stripped

    def test_strips_base64_format(self):
        b64 = "iVBORw0KGgo" + "A" * 150
        xml = (
            f'<mxfile><diagram name="P"><mxGraphModel><root>'
            f'<mxCell id="0"/>'
            f'<mxCell id="img1" style="image=data:image/png;base64,{b64}"/>'
            f"</root></mxGraphModel></diagram></mxfile>"
        )
        stripped, store = strip_embedded_images(xml)
        assert len(store.get("uris", {})) == 1
        assert b64 not in stripped

    def test_preserves_non_image_cells(self):
        xml = (
            '<mxfile><diagram name="P"><mxGraphModel><root>'
            '<mxCell id="0"/>'
            '<mxCell id="1" value="Hello" style="rounded=1"/>'
            "</root></mxGraphModel></diagram></mxfile>"
        )
        stripped, store = strip_embedded_images(xml)
        assert store == {}
        assert "Hello" in stripped

    def test_strips_multiple_images(self):
        b64a = "A" * 200
        b64b = "B" * 300
        xml = (
            f'<mxfile><diagram name="P"><mxGraphModel><root>'
            f'<mxCell id="0"/>'
            f'<mxCell id="img1" style="image=data:image/png,{b64a}"/>'
            f'<mxCell id="img2" style="image=data:image/jpeg,{b64b}"/>'
            f"</root></mxGraphModel></diagram></mxfile>"
        )
        stripped, store = strip_embedded_images(xml)
        assert len(store.get("uris", {})) == 2
        assert len(store.get("cells", {})) == 2


class TestRestoreEmbeddedImages:
    def test_restores_inline_placeholders(self):
        store = {
            "uris": {"__IMG_1__": "data:image/png,AAAA"},
            "cells": {},
        }
        xml = '<mxCell id="img1" style="image=__IMG_1__"/>'
        restored = restore_embedded_images(xml, store)
        assert "data:image/png,AAAA" in restored
        assert "__IMG_1__" not in restored

    def test_reinjects_dropped_cells(self):
        b64 = "A" * 200
        original_cell = f'<mxCell id="img1" style="shape=image;image=data:image/png,{b64}"/>'
        store = {
            "uris": {},
            "cells": {"img1": original_cell},
        }
        # LLM output without the image cell
        xml = (
            '<mxfile><diagram name="P"><mxGraphModel><root>'
            '<mxCell id="0"/>'
            '<mxCell id="1" value="Hello" style="rounded=1"/>'
            "</root></mxGraphModel></diagram></mxfile>"
        )
        restored = restore_embedded_images(xml, store)
        assert f"data:image/png,{b64}" in restored
        assert 'id="img1"' in restored

    def test_does_not_duplicate_existing_cells(self):
        b64 = "A" * 200
        original_cell = f'<mxCell id="img1" style="shape=image;image=data:image/png,{b64}"/>'
        store = {
            "uris": {"__IMG_1__": f"data:image/png,{b64}"},
            "cells": {"img1": original_cell},
        }
        # LLM kept the cell with placeholder
        xml = (
            '<mxfile><diagram name="P"><mxGraphModel><root>'
            '<mxCell id="0"/>'
            '<mxCell id="img1" style="shape=image;image=__IMG_1__"/>'
            "</root></mxGraphModel></diagram></mxfile>"
        )
        restored = restore_embedded_images(xml, store)
        # Should have exactly one img1 cell (not duplicated)
        assert restored.count('id="img1"') == 1
        assert f"data:image/png,{b64}" in restored

    def test_round_trip(self):
        b64 = "A" * 200
        original = (
            f'<mxfile><diagram name="P"><mxGraphModel><root>'
            f'<mxCell id="0"/>'
            f'<mxCell id="1" value="Hello" style="rounded=1"/>'
            f'<mxCell id="img1" style="shape=image;image=data:image/png,{b64}"/>'
            f"</root></mxGraphModel></diagram></mxfile>"
        )
        stripped, store = strip_embedded_images(original)
        restored = restore_embedded_images(stripped, store)
        assert f"data:image/png,{b64}" in restored
        assert "Hello" in restored


class TestPrepareEmbedImages:
    def test_creates_embed_entries(self, tmp_path):
        import io

        from PIL import Image

        img = Image.new("RGBA", (64, 48), (255, 0, 0, 255))
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        png_path = tmp_path / "icon.png"
        png_path.write_bytes(buf.getvalue())

        store, prompts = prepare_embed_images(
            files=[png_path],
            descriptions=["cloud server icon"],
        )

        assert "__IMG_1__" in store["uris"]
        assert store["uris"]["__IMG_1__"].startswith("data:image/png,")
        assert len(prompts) == 1
        assert "cloud server icon" in prompts[0]
        assert "64x48" in prompts[0]
        assert "icon.png" in prompts[0]

    def test_start_index_continues_numbering(self, tmp_path):
        import io

        from PIL import Image

        img = Image.new("RGBA", (32, 32), (0, 255, 0, 255))
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        p1 = tmp_path / "a.png"
        p2 = tmp_path / "b.png"
        p1.write_bytes(buf.getvalue())
        p2.write_bytes(buf.getvalue())

        store, prompts = prepare_embed_images(
            files=[p1, p2],
            descriptions=["icon A", "icon B"],
            start_index=5,
        )

        assert "__IMG_5__" in store["uris"]
        assert "__IMG_6__" in store["uris"]
        assert len(prompts) == 2

    def test_merges_with_existing_store(self, tmp_path):
        import io

        from PIL import Image

        b64_existing = "A" * 200
        xml = (
            f'<mxfile><diagram name="P"><mxGraphModel><root>'
            f'<mxCell id="0"/>'
            f'<mxCell id="1" value="Hello" style="rounded=1"/>'
            f'<mxCell id="img1" style="shape=image;image=data:image/png,{b64_existing}"/>'
            f"</root></mxGraphModel></diagram></mxfile>"
        )
        stripped, existing_store = strip_embedded_images(xml)
        assert "__IMG_1__" in stripped

        n_existing = len(existing_store.get("uris", {}))
        img = Image.new("RGBA", (48, 48), (0, 0, 255, 255))
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        embed_path = tmp_path / "embed.png"
        embed_path.write_bytes(buf.getvalue())

        embed_store, _prompts = prepare_embed_images(
            files=[embed_path],
            descriptions=["new icon"],
            start_index=n_existing + 1,
        )

        merged = {
            "cells": existing_store.get("cells", {}),
            "uris": {**existing_store.get("uris", {}), **embed_store["uris"]},
        }

        llm_xml = stripped.replace(
            "</root>",
            '<mxCell id="new1" style="shape=image;image=__IMG_2__" vertex="1" parent="1">'
            '<mxGeometry x="200" y="100" width="48" height="48" as="geometry"/>'
            "</mxCell></root>",
        )

        restored = restore_embedded_images(llm_xml, merged)
        assert f"data:image/png,{b64_existing}" in restored
        assert "data:image/png," in restored
        assert "__IMG_1__" not in restored
        assert "__IMG_2__" not in restored


class TestDecompressAndStripDrawio:
    def test_handles_uncompressed_xml(self):
        b64 = "A" * 200
        xml = (
            f'<mxfile><diagram name="P"><mxGraphModel><root>'
            f'<mxCell id="0" style="image=data:image/png,{b64}"/>'
            f"</root></mxGraphModel></diagram></mxfile>"
        )
        stripped, store = _decompress_and_strip_drawio(xml)
        assert store.get("uris")
        assert b64 not in stripped

    def test_handles_compressed_drawio(self):
        import base64
        import zlib

        inner = (
            '<mxGraphModel><root><mxCell id="0"/><mxCell id="1" value="Hello" style="rounded=1"/></root></mxGraphModel>'
        )
        compressed = base64.b64encode(zlib.compress(inner.encode("utf-8"))[2:-4]).decode()

        xml = f'<mxfile><diagram id="test" name="P">{compressed}</diagram></mxfile>'
        stripped, store = _decompress_and_strip_drawio(xml)
        assert "Hello" in stripped
        assert "mxGraphModel" in stripped

    def test_handles_invalid_xml(self):
        stripped, store = _decompress_and_strip_drawio("not xml")
        assert stripped == "not xml"
        assert store == {}


@pytest.mark.integration
class TestDiagramImageStrippingIntegration:
    def test_extract_strips_and_restore_roundtrips(self):
        import asyncio
        import base64
        import tempfile
        import zlib

        from document_agent.composition._diagram_source import (
            extract_diagram_source,
        )

        b64_img = "A" * 200
        inner = (
            f"<mxGraphModel><root>"
            f'<mxCell id="0"/>'
            f'<mxCell id="1" value="Test" style="rounded=1"/>'
            f'<mxCell id="img1" style="shape=image;image=data:image/png,{b64_img}"/>'
            f"</root></mxGraphModel>"
        )
        compressed = base64.b64encode(zlib.compress(inner.encode("utf-8"))[2:-4]).decode()
        xml = f'<mxfile><diagram id="t" name="P">{compressed}</diagram></mxfile>'

        tmp = Path(tempfile.mkdtemp()) / "test.drawio"
        tmp.write_text(xml, encoding="utf-8")

        source = asyncio.run(extract_diagram_source(tmp))
        assert source.diagram_type == "drawio"
        assert source.embedded_images is not None
        assert b64_img not in source.code
        assert "Test" in source.code

        # Simulate LLM dropping the image cell
        llm_output = source.code.replace('id="img1"', 'id="GONE"').replace("img1", "")
        restored = restore_embedded_images(llm_output, source.embedded_images)
        # Image cell should be re-injected
        assert f"data:image/png,{b64_img}" in restored

    def test_extract_from_drawio_png(self, tmp_path: Path):
        """Test extracting from a .drawio.png with embedded compressed XML."""

        from document_agent.composition._diagram_source import extract_diagram_source
        from document_agent.composition._drawio import (
            _embed_xml_in_png,
        )

        b64_img = "A" * 200
        inner_xml = (
            f'<mxfile><diagram id="t" name="P"><mxGraphModel><root>'
            f'<mxCell id="0"/>'
            f'<mxCell id="1" value="Hello" style="rounded=1"/>'
            f'<mxCell id="img1" style="shape=image;image=data:image/png,{b64_img}"/>'
            f"</root></mxGraphModel></diagram></mxfile>"
        )

        # Create a minimal PNG and embed the XML
        from conftest import _make_png_bytes

        png_path = tmp_path / "test.drawio.png"
        png_path.write_bytes(_make_png_bytes())
        _embed_xml_in_png(png_path, inner_xml)

        import asyncio

        source = asyncio.run(extract_diagram_source(png_path))
        assert source.diagram_type == "drawio"
        assert source.image_b64 is not None
        assert "Hello" in source.code
        # Images should be stripped
        assert b64_img not in source.code
        if source.embedded_images:
            assert source.embedded_images.get("cells")

    def test_llm_keeps_placeholders(self):
        """When LLM keeps __IMG_N__ placeholders, inline restore works."""
        b64 = "A" * 200
        xml = (
            f'<mxfile><diagram name="P"><mxGraphModel><root>'
            f'<mxCell id="0"/>'
            f'<mxCell id="1" value="Original" style="rounded=1"/>'
            f'<mxCell id="img1" style="shape=image;image=data:image/png,{b64}"/>'
            f"</root></mxGraphModel></diagram></mxfile>"
        )
        stripped, store = strip_embedded_images(xml)

        # Simulate LLM changing label but keeping image placeholder
        llm_output = stripped.replace("Original", "Updated")
        restored = restore_embedded_images(llm_output, store)

        assert "Updated" in restored
        assert f"data:image/png,{b64}" in restored
        assert "__IMG_" not in restored
        # Image cell not duplicated
        assert restored.count('id="img1"') == 1

    def test_llm_rewrites_xml_completely(self):
        """When LLM drops image cells entirely, they get re-injected."""
        b64 = "A" * 200
        xml = (
            f'<mxfile><diagram name="P"><mxGraphModel><root>'
            f'<mxCell id="0"/>'
            f'<mxCell id="1" value="Original" style="rounded=1"/>'
            f'<mxCell id="img1" style="shape=image;image=data:image/png,{b64}"/>'
            f"</root></mxGraphModel></diagram></mxfile>"
        )
        stripped, store = strip_embedded_images(xml)

        # Simulate LLM generating completely new XML without image cells
        llm_output = (
            '<mxfile><diagram name="P"><mxGraphModel><root>'
            '<mxCell id="0"/>'
            '<mxCell id="1" value="Rewritten" style="rounded=1"/>'
            '<mxCell id="2" value="New cell" style="ellipse"/>'
            "</root></mxGraphModel></diagram></mxfile>"
        )
        restored = restore_embedded_images(llm_output, store)

        assert "Rewritten" in restored
        assert "New cell" in restored
        # Image cell should be re-injected
        assert f"data:image/png,{b64}" in restored
        assert 'id="img1"' in restored

    def test_restored_xml_is_valid(self):
        """Restored XML should always be parseable."""
        from lxml import etree

        b64 = "A" * 200
        xml = (
            f'<mxfile><diagram name="P"><mxGraphModel><root>'
            f'<mxCell id="0"/>'
            f'<mxCell id="img1" style="shape=image;image=data:image/png,{b64}"/>'
            f"</root></mxGraphModel></diagram></mxfile>"
        )
        stripped, store = strip_embedded_images(xml)

        # Both paths: LLM keeps placeholder, LLM drops cell
        for scenario_xml in [stripped, stripped.replace('id="img1"', 'id="gone"')]:
            restored = restore_embedded_images(scenario_xml, store)
            root = etree.fromstring(restored.encode("utf-8"))
            assert root is not None


# Embed pipeline tests


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

        icon = _make_embed_png(tmp_path, "icon.png")

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

        call_args = mock_optimize.call_args
        assert call_args[0][1] == "drawio"


class TestEmbedPlacementInXml:
    """Test that embeds are correctly placed and restored in draw.io XML."""

    def test_embed_roundtrip_in_xml(self, tmp_path):
        """Simulate the full flow: prepare embeds, LLM places them, restore."""
        icon = _make_embed_png(tmp_path, "cloud.png", 64, 64)
        store, prompts = prepare_embed_images(
            files=[icon],
            descriptions=["cloud icon"],
        )

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

        assert "__IMG_1__" not in restored
        assert "data:image/png," in restored
        assert "Server" in restored

        from lxml import etree

        root = etree.fromstring(restored.encode("utf-8"))
        img_cells = [c for c in root.iter("mxCell") if "shape=image" in (c.get("style") or "")]
        assert len(img_cells) == 1
        style = img_cells[0].get("style")
        assert "image=data:image/png," in style
