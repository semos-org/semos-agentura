"""Unit tests for _render.py (pypdfium2-based page rendering)."""

import base64

import pytest
from document_agent._render import downscale_image_b64, render_document_pages


def _make_pdf(path, pages=2):
    """Write a minimal multi-page PDF using pypdf."""
    from pypdf import PdfWriter

    writer = PdfWriter()
    for _ in range(pages):
        writer.add_blank_page(width=300, height=400)
    with open(path, "wb") as f:
        writer.write(f)
    return path


class TestRenderPdf:
    def test_renders_one_png_per_page(self, tmp_path):
        pdf = _make_pdf(tmp_path / "doc.pdf", pages=3)
        out = tmp_path / "pages"
        pages = render_document_pages(pdf, out, dpi=72)
        assert len(pages) == 3
        for p in pages:
            assert p.exists()
            assert p.suffix == ".png"
            assert p.stat().st_size > 0

    def test_max_pages_caps_output(self, tmp_path):
        pdf = _make_pdf(tmp_path / "doc.pdf", pages=4)
        pages = render_document_pages(pdf, tmp_path / "p", dpi=72, max_pages=2)
        assert len(pages) == 2

    def test_unsupported_type_raises(self, tmp_path):
        txt = tmp_path / "x.txt"
        txt.write_text("hi")
        with pytest.raises(ValueError, match="Cannot render"):
            render_document_pages(txt, tmp_path / "p")


class TestDownscale:
    def test_returns_unchanged_when_under_budget(self):
        b64 = base64.b64encode(b"small").decode()
        assert downscale_image_b64(b64, 10_000) == b64

    def test_shrinks_large_image(self, tmp_path):
        from PIL import Image

        img = Image.new("RGB", (2000, 2000), (123, 50, 200))
        p = tmp_path / "big.png"
        img.save(p)
        raw = base64.b64encode(p.read_bytes()).decode()
        out = downscale_image_b64(raw, 50_000)
        assert len(out) <= 50_000
