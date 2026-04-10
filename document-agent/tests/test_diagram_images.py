"""Tests for draw.io embedded image stripping and restoration."""

from __future__ import annotations

from pathlib import Path

import pytest
from document_agent.composition._diagram_source import (
    _decompress_and_strip_drawio,
    restore_embedded_images,
    strip_embedded_images,
)


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
