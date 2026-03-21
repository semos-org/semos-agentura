"""Integration tests - require external tools (marp, pandoc, mmdc, libreoffice).

Mark tests that need specific tools so they can be skipped in CI if unavailable.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from document_agent._utils import find_tool, _project_root
from document_agent.composition._slides import _find_browser
from document_agent.config import Settings
from document_agent.exceptions import ToolNotFoundError
from document_agent.models import OutputFormat, OutputMode


# --- Tool availability markers ---

def _has_tool(name: str) -> bool:
    return find_tool(name) is not None


def _has_libre_office() -> bool:
    from document_agent.digestion._office import _find_libreoffice
    try:
        _find_libreoffice(None)
        return True
    except ToolNotFoundError:
        return False


needs_marp = pytest.mark.skipif(not _has_tool("marp"), reason="marp not installed")
needs_mmdc = pytest.mark.skipif(not _has_tool("mmdc"), reason="mmdc not installed")
needs_pandoc = pytest.mark.skipif(not _has_tool("pandoc"), reason="pandoc not installed")
needs_drawio = pytest.mark.skipif(not _has_tool("drawio"), reason="drawio not installed")
needs_libreoffice = pytest.mark.skipif(not _has_libre_office(), reason="LibreOffice not found")
needs_browser = pytest.mark.skipif(_find_browser() is None, reason="No browser for Marp PDF/PPTX")


# --- Mermaid rendering ---


@needs_mmdc
class TestMermaidRendering:
    def test_render_to_png(self, tmp_dir):
        from document_agent.composition._mermaid import render_mermaid_to_png

        mmdc = find_tool("mmdc")
        code = "graph TD\n  A[Start] --> B[End]"
        out = tmp_dir / "mermaid.png"
        render_mermaid_to_png(code, out, mmdc_path=mmdc)
        assert out.exists()
        assert out.stat().st_size > 100

    def test_render_to_base64(self, tmp_dir):
        from document_agent.composition._mermaid import render_mermaid_to_base64

        mmdc = find_tool("mmdc")
        code = "graph LR\n  X --> Y"
        result = render_mermaid_to_base64(code, mmdc_path=mmdc)
        assert result.startswith("data:image/png;base64,")

    def test_replace_blocks(self, tmp_dir):
        from document_agent.composition._mermaid import replace_mermaid_blocks

        mmdc = find_tool("mmdc")
        md = "# Title\n\n```mermaid\ngraph TD\n  A-->B\n```\n\nText"
        result = replace_mermaid_blocks(md, output_dir=tmp_dir, mmdc_path=mmdc)
        assert "```mermaid" not in result
        assert "mermaid_001.png" in result


# --- Draw.io rendering ---


@needs_drawio
class TestDrawioRendering:
    def test_render_to_png_with_embedding(self, tmp_dir):
        from document_agent.composition._drawio import (
            extract_xml_from_png,
            render_drawio_to_png,
        )

        drawio = find_tool("drawio")
        xml = (
            '<mxfile><diagram name="P">'
            "<mxGraphModel><root>"
            '<mxCell id="0"/><mxCell id="1" parent="0"/>'
            '<mxCell id="2" value="Box" style="rounded=1;" vertex="1" parent="1">'
            '<mxGeometry x="10" y="10" width="80" height="40" as="geometry"/>'
            "</mxCell>"
            "</root></mxGraphModel>"
            "</diagram></mxfile>"
        )
        out = tmp_dir / "drawio_test.drawio.png"
        render_drawio_to_png(xml, out, drawio_path=drawio)
        assert out.exists()
        assert out.stat().st_size > 100
        # Verify embedded XML can be extracted
        extracted = extract_xml_from_png(out)
        assert extracted is not None
        assert "Box" in extracted


# --- Slide generation ---


@needs_marp
class TestSlideGeneration:
    def test_html_output(self, tmp_dir):
        from document_agent.composition._slides import compose_slides

        marp = find_tool("marp")
        md = tmp_dir / "slides.md"
        md.write_text("---\nmarp: true\n---\n# Slide 1\nHello\n", encoding="utf-8")
        out = tmp_dir / "slides.html"
        compose_slides(md, out, OutputFormat.HTML, marp_path=marp)
        assert out.exists()
        assert "<html" in out.read_text(encoding="utf-8").lower()

    @needs_browser
    def test_pdf_output(self, tmp_dir):
        from document_agent.composition._slides import compose_slides

        marp = find_tool("marp")
        md = tmp_dir / "slides.md"
        md.write_text("---\nmarp: true\n---\n# Slide 1\n", encoding="utf-8")
        out = tmp_dir / "slides.pdf"
        compose_slides(md, out, OutputFormat.PDF, marp_path=marp)
        assert out.exists()
        assert out.stat().st_size > 100

    @needs_browser
    @needs_libreoffice
    def test_pptx_editable_output(self, tmp_dir):
        from document_agent.composition._slides import compose_slides

        marp = find_tool("marp")
        md = tmp_dir / "slides.md"
        md.write_text("---\nmarp: true\n---\n# Slide 1\nContent\n", encoding="utf-8")
        out = tmp_dir / "slides.pptx"
        compose_slides(md, out, OutputFormat.PPTX, marp_path=marp)
        assert out.exists()
        assert out.stat().st_size > 100


# --- Document generation ---


@needs_pandoc
class TestDocumentGeneration:
    def test_docx_output(self, tmp_dir):
        from document_agent.composition._documents import compose_document

        pandoc = find_tool("pandoc")
        md = tmp_dir / "doc.md"
        md.write_text("# Title\n\nParagraph text.\n\n- Item 1\n- Item 2\n", encoding="utf-8")
        out = tmp_dir / "doc.docx"
        compose_document(md, out, OutputFormat.DOCX, pandoc_path=pandoc)
        assert out.exists()
        assert out.stat().st_size > 100

    def test_html_output(self, tmp_dir):
        from document_agent.composition._documents import compose_document

        pandoc = find_tool("pandoc")
        md = tmp_dir / "doc.md"
        md.write_text("# Hello\n\nWorld\n", encoding="utf-8")
        out = tmp_dir / "doc.html"
        compose_document(md, out, OutputFormat.HTML, pandoc_path=pandoc)
        assert out.exists()


# --- Office to PDF conversion ---


@needs_libreoffice
class TestOfficeConversion:
    def test_docx_to_pdf(self, sample_docx):
        from document_agent.digestion._office import convert_office_to_pdf

        pdf = convert_office_to_pdf(sample_docx)
        try:
            assert pdf.exists()
            assert pdf.suffix == ".pdf"
            assert pdf.stat().st_size > 0
        finally:
            pdf.unlink(missing_ok=True)


# --- Browser auto-detection ---


class TestBrowserAutoDetection:
    def test_detection(self):
        result = _find_browser()
        if result is not None:
            assert result.is_file()
            assert result.suffix in (".exe", "")  # .exe on Windows, no ext on Linux


# --- CLI smoke tests ---


class TestCLI:
    def test_help(self):
        result = subprocess.run(
            ["uv", "run", "document-agent", "--help"],
            capture_output=True, text=True, timeout=30,
            cwd=str(_project_root()),
        )
        assert result.returncode == 0
        assert "digest" in result.stdout
        assert "compose" in result.stdout
        assert "inspect" in result.stdout
        assert "fill" in result.stdout
        assert "diagram" in result.stdout

    def test_digest_help(self):
        result = subprocess.run(
            ["uv", "run", "document-agent", "digest", "--help"],
            capture_output=True, text=True, timeout=30,
            cwd=str(_project_root()),
        )
        assert result.returncode == 0
        assert "--table-format" in result.stdout

    def test_inspect_docx(self, sample_docx):
        result = subprocess.run(
            ["uv", "run", "document-agent", "inspect", str(sample_docx)],
            capture_output=True, text=True, timeout=30,
            cwd=str(_project_root()),
        )
        assert result.returncode == 0
        assert "StartDate" in result.stdout

    def test_fill_docx(self, sample_docx, tmp_dir):
        out = tmp_dir / "cli_filled.docx"
        result = subprocess.run(
            [
                "uv", "run", "document-agent", "fill",
                str(sample_docx), str(out),
                "--data", '{"StartDate": "01.01.2027"}',
            ],
            capture_output=True, text=True, timeout=30,
            cwd=str(_project_root()),
        )
        assert result.returncode == 0
        assert out.exists()
