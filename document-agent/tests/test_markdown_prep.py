"""Tests for composition/_markdown_prep.py - base64 image extraction."""

import base64

from document_agent.composition._markdown_prep import (
    extract_base64_images,
    prepare_markdown_file,
)


class TestExtractBase64Images:
    def test_extracts_png(self, tmp_dir):
        b64 = base64.b64encode(b"\x89PNGfakedata").decode()
        md = f"# Title\n\n![Alt text](data:image/png;base64,{b64})\n\nText"
        result = extract_base64_images(md, tmp_dir)
        assert "data:image" not in result
        assert "_extracted_001.png" in result
        assert (tmp_dir / "_extracted_001.png").exists()

    def test_extracts_jpeg(self, tmp_dir):
        b64 = base64.b64encode(b"\xff\xd8\xff\xe0fakedata").decode()
        md = f"![](data:image/jpeg;base64,{b64})"
        result = extract_base64_images(md, tmp_dir)
        assert "_extracted_001.jpg" in result

    def test_multiple_images(self, tmp_dir):
        b64 = base64.b64encode(b"fake").decode()
        md = f"![a](data:image/png;base64,{b64})\n![b](data:image/png;base64,{b64})"
        result = extract_base64_images(md, tmp_dir)
        assert "_extracted_001.png" in result
        assert "_extracted_002.png" in result

    def test_no_images_passthrough(self, tmp_dir):
        md = "# Just text\nNo images here"
        assert extract_base64_images(md, tmp_dir) == md

    def test_preserves_alt_text(self, tmp_dir):
        b64 = base64.b64encode(b"fake").decode()
        md = f"![My description](data:image/png;base64,{b64})"
        result = extract_base64_images(md, tmp_dir)
        assert "![My description]" in result


class TestPrepareMarkdownFile:
    def test_from_path(self, tmp_dir):
        src = tmp_dir / "input.md"
        src.write_text("# Hello\nWorld", encoding="utf-8")
        work = tmp_dir / "work"
        work.mkdir()
        result = prepare_markdown_file(src, work)
        assert result.exists()
        assert result.read_text(encoding="utf-8") == "# Hello\nWorld"

    def test_from_string(self, tmp_dir):
        work = tmp_dir / "work"
        work.mkdir()
        result = prepare_markdown_file("# Inline\nContent", work)
        assert result.name == "input.md"
        assert "# Inline" in result.read_text(encoding="utf-8")

    def test_extracts_images_from_string(self, tmp_dir):
        b64 = base64.b64encode(b"img").decode()
        md = f"![](data:image/png;base64,{b64})"
        work = tmp_dir / "work"
        work.mkdir()
        result = prepare_markdown_file(md, work)
        content = result.read_text(encoding="utf-8")
        assert "data:image" not in content
        assert "_extracted_" in content
