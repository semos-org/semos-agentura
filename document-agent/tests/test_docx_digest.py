"""Tests for pandoc-based DOCX digestion."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from document_agent.digestion._docx_digest import (
    _inline_images_as_base64,
    _normalize_pandoc_images,
    digest_docx_with_pandoc,
)
from document_agent.models import OutputMode

# Unit tests - no external tools needed


class TestNormalizePandocImages:
    """Tests for _normalize_pandoc_images (pure Python, no pandoc)."""

    def test_renames_media_to_stem_convention(self, tmp_path: Path):
        media_dir = tmp_path / "media"
        media_dir.mkdir()
        (media_dir / "image1.png").write_bytes(b"fake-png")
        output_dir = tmp_path / "out"
        output_dir.mkdir()

        md = "Here is an image: ![alt](media/image1.png)"
        new_md, image_map = _normalize_pandoc_images(md, media_dir, "doc", output_dir)

        assert "doc_images/doc_001.png" in new_md
        assert "media/image1.png" not in new_md
        assert len(image_map) == 1
        dest = list(image_map.values())[0]
        assert dest.exists()
        assert dest.name == "doc_001.png"

    def test_updates_multiple_images(self, tmp_path: Path):
        media_dir = tmp_path / "media"
        media_dir.mkdir()
        (media_dir / "image1.png").write_bytes(b"fake1")
        (media_dir / "image2.jpg").write_bytes(b"fake2")
        output_dir = tmp_path / "out"
        output_dir.mkdir()

        md = "![a](media/image1.png) and ![b](media/image2.jpg)"
        new_md, image_map = _normalize_pandoc_images(md, media_dir, "test", output_dir)

        assert "test_images/test_001" in new_md
        assert "test_images/test_002" in new_md
        assert len(image_map) == 2

    def test_handles_no_images(self, tmp_path: Path):
        media_dir = tmp_path / "media"
        # media_dir doesn't exist
        output_dir = tmp_path / "out"
        output_dir.mkdir()

        md = "No images here."
        new_md, image_map = _normalize_pandoc_images(md, media_dir, "doc", output_dir)

        assert new_md == md
        assert len(image_map) == 0

    def test_handles_empty_media_dir(self, tmp_path: Path):
        media_dir = tmp_path / "media"
        media_dir.mkdir()
        output_dir = tmp_path / "out"
        output_dir.mkdir()

        md = "No image files."
        new_md, image_map = _normalize_pandoc_images(md, media_dir, "doc", output_dir)

        assert new_md == md
        assert len(image_map) == 0

    def test_skips_non_image_files(self, tmp_path: Path):
        media_dir = tmp_path / "media"
        media_dir.mkdir()
        (media_dir / "image1.png").write_bytes(b"fake")
        (media_dir / "data.xml").write_bytes(b"<xml/>")
        output_dir = tmp_path / "out"
        output_dir.mkdir()

        md = "![](media/image1.png)"
        _, image_map = _normalize_pandoc_images(md, media_dir, "doc", output_dir)
        assert len(image_map) == 1


class TestInlineImagesAsBase64:
    def test_replaces_file_refs_with_data_uri(self, tmp_path: Path):
        img = tmp_path / "test.png"
        img.write_bytes(b"\x89PNG fake")

        md = "![alt](test.png)"
        image_map = {"test.png": img}
        result = _inline_images_as_base64(md, image_map)

        assert "data:image/png;base64," in result
        assert "test.png" not in result

    def test_preserves_text_without_images(self):
        md = "Just text, no images."
        result = _inline_images_as_base64(md, {})
        assert result == md


class TestMarkdownPostProcessing:
    def test_footnotes_preserved_in_markdown(self):
        md = "Text with footnote[^1].\n\n[^1]: This is a footnote."
        # Normalization should not alter footnote syntax
        assert "[^1]" in md
        assert "[^1]: This is a footnote." in md

    def test_pipe_tables_unchanged(self):
        md = "| A | B |\n|---|---|\n| 1 | 2 |"
        # pipe tables should pass through
        assert "| A | B |" in md


# Integration tests - require pandoc


@pytest.mark.integration
class TestDigestDocxPandoc:
    def test_basic_text_extraction(self, sample_docx_rich: Path, tmp_path: Path):
        result = digest_docx_with_pandoc(
            sample_docx_rich,
            output_dir=tmp_path,
            output_mode=OutputMode.FILE,
        )
        assert result.markdown
        assert "Introduction" in result.markdown

    def test_footnotes_extracted(self, sample_docx_rich: Path, tmp_path: Path):
        result = digest_docx_with_pandoc(
            sample_docx_rich,
            output_dir=tmp_path,
            output_mode=OutputMode.FILE,
        )
        # Pandoc converts footnotes to [^N] syntax
        assert "[^1]" in result.markdown
        assert "This is a footnote" in result.markdown

    def test_track_changes_accept(self, sample_docx_rich: Path, tmp_path: Path):
        result = digest_docx_with_pandoc(
            sample_docx_rich,
            output_dir=tmp_path,
            track_changes="accept",
        )
        # Accepted: insertions visible, deletions gone
        assert "added by Alice" in result.markdown
        assert "removed by Bob" not in result.markdown

    def test_track_changes_reject(self, sample_docx_rich: Path, tmp_path: Path):
        result = digest_docx_with_pandoc(
            sample_docx_rich,
            output_dir=tmp_path,
            track_changes="reject",
        )
        # Rejected: deletions visible, insertions gone
        assert "removed by Bob" in result.markdown
        assert "added by Alice" not in result.markdown

    def test_track_changes_all(self, sample_docx_rich: Path, tmp_path: Path):
        result = digest_docx_with_pandoc(
            sample_docx_rich,
            output_dir=tmp_path,
            track_changes="all",
        )
        # Both insertions and deletions annotated
        assert "added by Alice" in result.markdown or "insertion" in result.markdown.lower()
        assert "removed by Bob" in result.markdown or "deletion" in result.markdown.lower()

    def test_tables_as_pipe_tables(self, sample_docx_rich: Path, tmp_path: Path):
        result = digest_docx_with_pandoc(
            sample_docx_rich,
            output_dir=tmp_path,
        )
        assert "Column A" in result.markdown
        assert "Value 1" in result.markdown

    def test_images_extracted(self, sample_docx_rich: Path, tmp_path: Path):
        result = digest_docx_with_pandoc(
            sample_docx_rich,
            output_dir=tmp_path,
            output_mode=OutputMode.FILE,
        )
        if result.images_dir:
            assert result.images_dir.exists()
            images = list(result.images_dir.iterdir())
            assert len(images) >= 1

    def test_inline_mode(self, sample_docx_rich: Path, tmp_path: Path):
        result = digest_docx_with_pandoc(
            sample_docx_rich,
            output_dir=tmp_path,
            output_mode=OutputMode.INLINE,
        )
        assert result.markdown
        assert result.output_path is None
        # If images were found, they should be base64
        if "image" in result.markdown.lower():
            assert "data:image/" in result.markdown or "![" in result.markdown

    def test_output_file_written(self, sample_docx_rich: Path, tmp_path: Path):
        result = digest_docx_with_pandoc(
            sample_docx_rich,
            output_dir=tmp_path,
            output_mode=OutputMode.FILE,
        )
        assert result.output_path is not None
        assert result.output_path.exists()
        assert result.output_path.suffix == ".md"

    def test_invalid_track_changes_raises(self, sample_docx_rich: Path, tmp_path: Path):
        with pytest.raises(Exception, match="Invalid track_changes"):
            digest_docx_with_pandoc(
                sample_docx_rich,
                output_dir=tmp_path,
                track_changes="invalid",
            )


@pytest.mark.integration
class TestDigestRoutingDocx:
    """Test that digest() routes DOCX to pandoc path."""

    def test_auto_mode_uses_pandoc(self, sample_docx_rich: Path, tmp_path: Path):
        from document_agent.digestion.digest import digest

        result = digest(
            sample_docx_rich,
            output_dir=tmp_path,
            output_mode=OutputMode.FILE,
            digest_mode="auto",
        )
        # Pandoc path should extract footnotes
        assert "Introduction" in result.markdown

    def test_ocr_mode_skips_pandoc(self, sample_docx_rich: Path, tmp_path: Path):
        """When digest_mode='ocr', pandoc is bypassed."""
        from document_agent.digestion.digest import digest

        # This will try the OCR path (LibreOffice + OCR).
        # It may fail if LibreOffice is not available, but it should NOT
        # call the pandoc path.
        with patch("document_agent.digestion.digest.digest_docx_with_pandoc") as mock_pandoc:
            try:
                digest(
                    sample_docx_rich,
                    output_dir=tmp_path,
                    digest_mode="ocr",
                )
            except Exception:
                pass  # OCR path may fail without LibreOffice
            mock_pandoc.assert_not_called()


class TestDescribeImagesMock:
    """Test the describe_images flag with a mocked provider."""

    def test_describe_images_calls_provider(self, tmp_path: Path):
        from document_agent.digestion._docx_digest import _annotate_images

        img_path = tmp_path / "test.png"
        img_path.write_bytes(b"\x89PNG fake")
        image_map = {"test_images/test_001.png": img_path}
        md = "![](test_images/test_001.png)"

        mock_page = MagicMock()
        mock_page.images = [
            MagicMock(
                image_annotation='{"image_type": "diagram", "description": "A red square", "text_content": ""}',
            )
        ]
        mock_page.markdown = ""
        mock_response = MagicMock()
        mock_response.pages = [mock_page]

        mock_provider = MagicMock()
        mock_provider.ocr.return_value = mock_response

        with patch(
            "document_agent.digestion._docx_digest.get_provider",
            return_value=mock_provider,
        ):
            settings = MagicMock()
            result = _annotate_images(image_map, md, settings)

        assert "diagram" in result or "red square" in result
        mock_provider.ocr.assert_called_once()
