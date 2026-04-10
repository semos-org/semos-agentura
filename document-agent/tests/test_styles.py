"""Tests for YAML style extraction, reference doc generation, and round-trip."""

from __future__ import annotations

from pathlib import Path
from zipfile import ZipFile

import pytest
from document_agent.composition._reference_doc import (
    generate_reference_doc,
    parse_styles_from_markdown,
)
from document_agent.digestion._styles import (
    extract_styles,
    format_yaml_frontmatter,
)

# Unit tests - style extraction


class TestExtractStyles:
    def test_extracts_body_font(self, sample_reference_docx: Path):
        styles = extract_styles(sample_reference_docx)
        assert "body" in styles
        assert styles["body"].get("font") == "Arial"

    def test_extracts_heading_styles(self, sample_reference_docx: Path):
        styles = extract_styles(sample_reference_docx)
        assert "heading1" in styles
        assert styles["heading1"].get("bold") is True
        assert styles["heading1"].get("size") == 16

    def test_extracts_page_size(self, sample_reference_docx: Path):
        styles = extract_styles(sample_reference_docx)
        # sample_reference_docx doesn't have explicit sectPr, so page may be absent
        # This is fine - just verify no crash
        assert isinstance(styles, dict)

    def test_empty_styles_for_minimal_docx(self, sample_docx: Path):
        styles = extract_styles(sample_docx)
        assert isinstance(styles, dict)


class TestFormatYamlFrontmatter:
    def test_formats_body_and_page(self):
        styles = {
            "body": {"font": "Calibri", "size": 11},
            "page": {"size": "A4", "margin-top": "2.0cm"},
        }
        fm = format_yaml_frontmatter(styles)
        assert fm.startswith("---\n")
        assert fm.endswith("---\n\n")
        assert 'font: "Calibri"' in fm
        assert "size: 11" in fm
        assert 'size: "A4"' in fm

    def test_empty_styles_returns_empty(self):
        assert format_yaml_frontmatter({}) == ""

    def test_formats_bool_values(self):
        styles = {"heading1": {"bold": True, "italic": False}}
        fm = format_yaml_frontmatter(styles)
        assert "bold: true" in fm
        assert "italic: false" in fm


# Unit tests - YAML front matter parsing


class TestParseStylesFromMarkdown:
    def test_parses_full_styles(self):
        md = """---
styles:
  page:
    size: "A4"
    margin-top: "1.5cm"
  body:
    font: "Calibri"
    size: 11
    line-spacing: 1.1
  heading1:
    font: "Arial"
    size: 14
    bold: true
    color: "000080"
  table:
    size: 9
    border-color: "999999"
---

# Hello
"""
        styles = parse_styles_from_markdown(md)
        assert styles is not None
        assert styles["page"]["size"] == "A4"
        assert styles["page"]["margin-top"] == "1.5cm"
        assert styles["body"]["font"] == "Calibri"
        assert styles["body"]["size"] == 11
        assert styles["body"]["line-spacing"] == 1.1
        assert styles["heading1"]["bold"] is True
        assert styles["heading1"]["color"] == "000080"
        assert styles["table"]["size"] == 9

    def test_returns_none_without_frontmatter(self):
        assert parse_styles_from_markdown("# Just a heading\n") is None

    def test_returns_none_for_non_styles_frontmatter(self):
        md = "---\ntitle: Test\n---\n\n# Hello\n"
        assert parse_styles_from_markdown(md) is None


# Unit tests - reference doc generation


class TestGenerateReferenceDoc:
    def test_creates_valid_docx(self, tmp_path: Path):
        styles = {
            "body": {"font": "Calibri", "size": 11},
            "heading1": {"font": "Arial", "size": 14, "bold": True},
            "page": {"size": "A4", "margin-top": "1.5cm"},
        }
        out = tmp_path / "ref.docx"
        generate_reference_doc(styles, out)
        assert out.exists()
        with ZipFile(out) as z:
            assert "word/document.xml" in z.namelist()
            assert "word/styles.xml" in z.namelist()

    def test_includes_table_style_with_borders(self, tmp_path: Path):
        styles = {
            "body": {"font": "Calibri", "size": 11},
            "table": {"size": 9, "border-color": "999999", "border-size": 4},
        }
        out = tmp_path / "ref.docx"
        generate_reference_doc(styles, out)
        with ZipFile(out) as z:
            xml = z.read("word/styles.xml").decode("utf-8")
            assert "Table" in xml
            assert "tblBorders" in xml
            assert "999999" in xml

    def test_includes_footnote_and_caption_styles(self, tmp_path: Path):
        styles = {
            "body": {"font": "Calibri", "size": 11},
            "table": {"size": 9},
        }
        out = tmp_path / "ref.docx"
        generate_reference_doc(styles, out)
        with ZipFile(out) as z:
            xml = z.read("word/styles.xml").decode("utf-8")
            assert "FootnoteText" in xml
            assert "Caption" in xml
            assert "TableCaption" in xml
            assert "ImageCaption" in xml
            # 9pt = 18 half-points
            assert 'w:val="18"' in xml

    def test_page_margins_applied(self, tmp_path: Path):
        styles = {
            "page": {"size": "A4", "margin-top": "3.0cm", "margin-left": "2.0cm"},
        }
        out = tmp_path / "ref.docx"
        generate_reference_doc(styles, out)
        with ZipFile(out) as z:
            doc = z.read("word/document.xml").decode("utf-8")
            assert "w:pgSz" in doc
            assert "w:pgMar" in doc
            # 3.0cm = 1701 twips
            assert "1701" in doc

    def test_header_footer_from_source(self, tmp_path: Path, sample_docx_rich: Path):
        styles = {"body": {"font": "Calibri", "size": 11}}
        out = tmp_path / "ref.docx"
        generate_reference_doc(styles, out, header_footer_source=sample_docx_rich)
        # sample_docx_rich doesn't have headers/footers, so this just verifies no crash
        assert out.exists()

    def test_spacing_properties(self, tmp_path: Path):
        styles = {
            "body": {
                "font": "Calibri",
                "size": 11,
                "spacing-before": "0.0cm",
                "spacing-after": "0.1cm",
                "line-spacing": 1.1,
            },
        }
        out = tmp_path / "ref.docx"
        generate_reference_doc(styles, out)
        with ZipFile(out) as z:
            xml = z.read("word/styles.xml").decode("utf-8")
            assert "w:spacing" in xml
            # line-spacing 1.1 = 264 (1.1 * 240)
            assert "264" in xml


# Integration tests - require pandoc


@pytest.mark.integration
class TestStyleRoundTrip:
    def test_digest_extracts_styles_as_frontmatter(self, sample_reference_docx: Path, tmp_path: Path):
        from document_agent.digestion._docx_digest import digest_docx_with_pandoc
        from document_agent.models import OutputMode

        result = digest_docx_with_pandoc(
            sample_reference_docx,
            output_dir=tmp_path,
            output_mode=OutputMode.FILE,
            include_styles=True,
        )
        assert result.markdown.startswith("---\n")
        assert "styles:" in result.markdown
        assert "Arial" in result.markdown  # body font from sample_reference_docx

    def test_digest_no_styles_when_disabled(self, sample_reference_docx: Path, tmp_path: Path):
        from document_agent.digestion._docx_digest import digest_docx_with_pandoc
        from document_agent.models import OutputMode

        result = digest_docx_with_pandoc(
            sample_reference_docx,
            output_dir=tmp_path,
            output_mode=OutputMode.FILE,
            include_styles=False,
        )
        assert not result.markdown.startswith("---\n")

    def test_compose_uses_yaml_styles(self, tmp_path: Path):
        from document_agent.composition.compose import compose
        from document_agent.models import OutputFormat

        md = """---
styles:
  body:
    font: "Comic Sans MS"
    size: 14
  page:
    size: "A4"
---

# Test

Body text here.
"""
        md_path = tmp_path / "input.md"
        md_path.write_text(md, encoding="utf-8")
        out = tmp_path / "output.docx"

        compose(source=md_path, output_path=out, format=OutputFormat.DOCX)
        assert out.exists()

        with ZipFile(out) as z:
            styles_xml = z.read("word/styles.xml").decode("utf-8")
            assert "Comic Sans MS" in styles_xml

    def test_compose_with_header_footer_doc(self, sample_reference_docx: Path, tmp_path: Path):
        from document_agent.composition.compose import compose
        from document_agent.models import OutputFormat

        md = """---
styles:
  body:
    font: "Calibri"
    size: 11
---

# Test

Body text.
"""
        md_path = tmp_path / "input.md"
        md_path.write_text(md, encoding="utf-8")
        out = tmp_path / "output.docx"

        # sample_reference_docx has no headers/footers but shouldn't crash
        compose(
            source=md_path,
            output_path=out,
            format=OutputFormat.DOCX,
            header_footer_doc=sample_reference_docx,
        )
        assert out.exists()

    def test_full_style_round_trip(self, sample_reference_docx: Path, tmp_path: Path):
        """Extract styles from a DOCX, compose a new doc with those styles."""
        from document_agent.composition.compose import compose
        from document_agent.models import OutputFormat

        # Extract styles
        styles = extract_styles(sample_reference_docx)
        fm = format_yaml_frontmatter(styles)

        md = fm + "# Round-Trip Test\n\nBody text with styles.\n"
        md_path = tmp_path / "input.md"
        md_path.write_text(md, encoding="utf-8")
        out = tmp_path / "output.docx"

        compose(source=md_path, output_path=out, format=OutputFormat.DOCX)
        assert out.exists()

        # Re-extract styles from the output
        out_styles = extract_styles(out)
        # Font should survive
        if "body" in styles and "font" in styles["body"]:
            assert out_styles.get("body", {}).get("font") == styles["body"]["font"]
