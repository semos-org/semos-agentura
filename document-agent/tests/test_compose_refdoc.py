"""Tests for compose with reference_doc and footnotes."""

from __future__ import annotations

from pathlib import Path
from zipfile import ZipFile

import pytest
from document_agent.models import OutputFormat


@pytest.mark.integration
class TestComposeReferenceDoc:
    """Integration tests - require pandoc."""

    def test_compose_docx_with_reference_doc(self, sample_reference_docx: Path, tmp_path: Path):
        from document_agent.composition.compose import compose

        md = "# Hello\n\nThis is a test document.\n"
        md_path = tmp_path / "input.md"
        md_path.write_text(md, encoding="utf-8")
        out_path = tmp_path / "output.docx"

        result = compose(
            source=md_path,
            output_path=out_path,
            format=OutputFormat.DOCX,
            reference_doc=sample_reference_docx,
        )
        assert result.output_path.exists()
        assert result.output_path.suffix == ".docx"
        # Verify it's a valid DOCX
        with ZipFile(result.output_path) as z:
            assert "word/document.xml" in z.namelist()

    def test_compose_without_reference_doc_unchanged(self, tmp_path: Path):
        from document_agent.composition.compose import compose

        md = "# Test\n\nBody text.\n"
        md_path = tmp_path / "input.md"
        md_path.write_text(md, encoding="utf-8")
        out_path = tmp_path / "output.docx"

        result = compose(
            source=md_path,
            output_path=out_path,
            format=OutputFormat.DOCX,
        )
        assert result.output_path.exists()

    def test_reference_doc_ignored_for_pdf(self, sample_reference_docx: Path, tmp_path: Path):
        """reference_doc should be silently ignored for non-DOCX/ODT formats."""
        from document_agent._utils import find_tool
        from document_agent.composition._documents import compose_document

        pandoc = find_tool("pandoc")
        if pandoc is None:
            pytest.skip("pandoc not found")

        md = tmp_path / "input.md"
        md.write_text("# Test\n\nBody.\n", encoding="utf-8")
        out = tmp_path / "output.html"

        # Should not raise even though reference_doc is provided
        compose_document(
            md,
            out,
            OutputFormat.HTML,
            pandoc_path=pandoc,
            reference_doc=sample_reference_docx,
        )
        assert out.exists()


@pytest.mark.integration
class TestFootnotesRoundTrip:
    """Test that footnotes survive digest -> compose cycle."""

    def test_footnotes_in_compose(self, tmp_path: Path):
        """Markdown with [^1] footnotes produces DOCX with footnotes."""
        from document_agent.composition.compose import compose

        md = "This has a footnote[^1].\n\n[^1]: The footnote content.\n"
        md_path = tmp_path / "input.md"
        md_path.write_text(md, encoding="utf-8")
        out_path = tmp_path / "output.docx"

        result = compose(
            source=md_path,
            output_path=out_path,
            format=OutputFormat.DOCX,
        )
        assert result.output_path.exists()
        # Verify DOCX contains footnotes.xml
        with ZipFile(result.output_path) as z:
            names = z.namelist()
            assert "word/footnotes.xml" in names
            fn_xml = z.read("word/footnotes.xml").decode("utf-8")
            assert "footnote content" in fn_xml.lower() or "footnote" in fn_xml.lower()

    def test_footnote_round_trip(self, sample_docx_rich: Path, tmp_path: Path):
        """Digest DOCX with footnotes, then compose back - footnotes survive."""
        from document_agent.composition.compose import compose
        from document_agent.digestion._docx_digest import digest_docx_with_pandoc
        from document_agent.models import OutputMode

        # Step 1: Digest DOCX to Markdown
        result1 = digest_docx_with_pandoc(
            sample_docx_rich,
            output_dir=tmp_path / "digest",
            output_mode=OutputMode.FILE,
        )
        assert "[^1]" in result1.markdown
        assert "footnote" in result1.markdown.lower()

        # Step 2: Compose Markdown back to DOCX
        md_path = result1.output_path
        out_docx = tmp_path / "round_trip.docx"
        result2 = compose(
            source=md_path,
            output_path=out_docx,
            format=OutputFormat.DOCX,
        )
        assert result2.output_path.exists()

        # Step 3: Verify the output DOCX has footnotes
        with ZipFile(result2.output_path) as z:
            assert "word/footnotes.xml" in z.namelist()

        # Step 4: Digest the round-tripped DOCX again
        result3 = digest_docx_with_pandoc(
            result2.output_path,
            output_dir=tmp_path / "digest2",
            output_mode=OutputMode.FILE,
        )
        assert "[^1]" in result3.markdown
        assert "footnote" in result3.markdown.lower()
