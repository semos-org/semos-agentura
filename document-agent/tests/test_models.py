"""Tests for models.py - enums, dataclasses, Pydantic models."""

from pathlib import Path

from document_agent.models import (
    ComposeResult,
    DiagramResult,
    DigestResult,
    ImageDescription,
    OutputFormat,
    OutputMode,
)


class TestOutputFormat:
    def test_values(self):
        assert OutputFormat.PDF == "pdf"
        assert OutputFormat.PPTX == "pptx"
        assert OutputFormat.DOCX == "docx"
        assert OutputFormat.HTML == "html"

    def test_from_string(self):
        assert OutputFormat("pdf") == OutputFormat.PDF


class TestOutputMode:
    def test_values(self):
        assert OutputMode.FILE == "file"
        assert OutputMode.INLINE == "inline"


class TestDigestResult:
    def test_defaults(self):
        r = DigestResult(markdown="# Hello")
        assert r.markdown == "# Hello"
        assert r.output_path is None
        assert r.images_dir is None
        assert r.annotation is None

    def test_with_all_fields(self):
        r = DigestResult(
            markdown="text",
            output_path=Path("/tmp/out.md"),
            images_dir=Path("/tmp/images"),
            annotation={"key": "val"},
            annotation_path=Path("/tmp/ann.json"),
        )
        assert r.output_path == Path("/tmp/out.md")


class TestComposeResult:
    def test_defaults(self):
        r = ComposeResult()
        assert r.output_path is None
        assert r.format == OutputFormat.PDF


class TestDiagramResult:
    def test_review_log_default(self):
        r = DiagramResult(code="graph TD", image_path=Path("x.png"), iterations=1)
        assert r.review_log == []


class TestImageDescription:
    def test_schema(self):
        img = ImageDescription(
            image_type="photo",
            text_content="Hello World",
            description="A greeting",
        )
        assert img.image_type == "photo"
        d = img.model_dump()
        assert "image_type" in d
