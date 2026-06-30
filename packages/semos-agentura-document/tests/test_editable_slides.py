"""Tests for editable slides (Marp/pandoc -> PPTX) pipeline."""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest
from semos.agentura.document._utils import find_tool
from semos.agentura.document.composition._editable_slides import (
    _convert_html_columns,
    _is_marp,
    parse_marp,
    slide_to_pandoc,
)

# ------------------------------------------------------------------
# Fixtures
# ------------------------------------------------------------------

MARP_BASIC = textwrap.dedent("""\
    ---
    marp: true
    ---

    <!-- _class: title -->
    # Title Slide
    ## Subtitle

    ---

    # Content Slide

    - Bullet one
    - Bullet two

    ---

    # Image Slide

    ![w:800](diagram.png)

    Some text below.
""")

MARP_BG_IMAGE = textwrap.dedent("""\
    ---
    marp: true
    ---

    # Split Layout

    ![bg right:48% fit](photo.jpg)

    - Left text
    - More text
""")

MARP_WITH_TABLE = textwrap.dedent("""\
    ---
    marp: true
    ---

    # Table Slide

    Description text here.

    | Col1 | Col2 |
    |------|------|
    | A    | B    |
""")

MARP_HTML_COLUMNS = textwrap.dedent("""\
    ---
    marp: true
    ---

    # Two Columns

    <div class="columns">
    <div>

    Left content

    </div>
    <div>

    Right content

    </div>
    </div>
""")

MARP_WITH_NOTES = textwrap.dedent("""\
    ---
    marp: true
    ---

    # Slide With Notes

    Body text.

    <!-- Speaker notes go here.
    Multiple lines. -->
""")

PANDOC_SLIDES = textwrap.dedent("""\
    # Title

    ## Subtitle

    Author

    # Slide One

    - Point A
    - Point B
""")


def _create_dummy_png(path: Path, width: int = 200, height: int = 100) -> None:
    """Create a minimal valid PNG file (solid color)."""
    import struct
    import zlib

    def _chunk(chunk_type: bytes, data: bytes) -> bytes:
        c = chunk_type + data
        return struct.pack(">I", len(data)) + c + struct.pack(">I", zlib.crc32(c) & 0xFFFFFFFF)

    sig = b"\x89PNG\r\n\x1a\n"
    ihdr = _chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
    # Single row of blue pixels, repeated
    raw_row = b"\x00" + b"\x33\x66\x99" * width
    raw = raw_row * height
    idat = _chunk(b"IDAT", zlib.compress(raw))
    iend = _chunk(b"IEND", b"")
    path.write_bytes(sig + ihdr + idat + iend)


@pytest.fixture
def marp_file(tmp_path: Path) -> Path:
    p = tmp_path / "slides.md"
    p.write_text(MARP_BASIC, encoding="utf-8")
    _create_dummy_png(tmp_path / "diagram.png", 800, 400)
    return p


@pytest.fixture
def pandoc_file(tmp_path: Path) -> Path:
    p = tmp_path / "slides.md"
    p.write_text(PANDOC_SLIDES, encoding="utf-8")
    return p


# ------------------------------------------------------------------
# _is_marp detection
# ------------------------------------------------------------------


class TestIsMarp:
    def test_detects_marp(self, tmp_path: Path) -> None:
        f = tmp_path / "marp.md"
        f.write_text(MARP_BASIC, encoding="utf-8")
        assert _is_marp(f) is True

    def test_detects_pandoc(self, tmp_path: Path) -> None:
        f = tmp_path / "pandoc.md"
        f.write_text(PANDOC_SLIDES, encoding="utf-8")
        assert _is_marp(f) is False

    def test_no_frontmatter(self, tmp_path: Path) -> None:
        f = tmp_path / "plain.md"
        f.write_text("# Just a heading\n\nBody.\n", encoding="utf-8")
        assert _is_marp(f) is False

    def test_frontmatter_without_marp(self, tmp_path: Path) -> None:
        f = tmp_path / "other.md"
        f.write_text("---\ntitle: Hello\n---\n\n# Slide\n", encoding="utf-8")
        assert _is_marp(f) is False

    def test_marp_in_multiline_style(self, tmp_path: Path) -> None:
        """marp: true even with long style block in frontmatter."""
        f = tmp_path / "long.md"
        f.write_text(
            "---\nmarp: true\nstyle: |\n  section { font-size: 24px; }\n"
            "  h1 { color: red; }\n  h2 { color: blue; }\n---\n\n# S\n",
            encoding="utf-8",
        )
        assert _is_marp(f) is True


# ------------------------------------------------------------------
# parse_marp
# ------------------------------------------------------------------


class TestParseMarp:
    def test_basic_slide_count(self, marp_file: Path) -> None:
        slides = parse_marp(marp_file)
        assert len(slides) == 3

    def test_title_slide(self, marp_file: Path) -> None:
        slides = parse_marp(marp_file)
        assert slides[0]["class"] == "title"
        assert slides[0]["title"] == "Title Slide"

    def test_content_slide(self, marp_file: Path) -> None:
        slides = parse_marp(marp_file)
        assert slides[1]["title"] == "Content Slide"
        assert any("Bullet one" in ln for ln in slides[1]["body_lines"])

    def test_inline_image(self, marp_file: Path) -> None:
        slides = parse_marp(marp_file)
        assert slides[2]["inline_images"] == ["diagram.png"]
        assert any("text below" in ln for ln in slides[2]["body_lines"])

    def test_bg_image(self, tmp_path: Path) -> None:
        f = tmp_path / "bg.md"
        f.write_text(MARP_BG_IMAGE, encoding="utf-8")
        slides = parse_marp(f)
        assert len(slides[0]["bg_images"]) == 1
        bg = slides[0]["bg_images"][0]
        assert bg["side"] == "right"
        assert bg["pct"] == 48
        assert bg["path"] == "photo.jpg"

    def test_speaker_notes(self, tmp_path: Path) -> None:
        f = tmp_path / "notes.md"
        f.write_text(MARP_WITH_NOTES, encoding="utf-8")
        slides = parse_marp(f)
        assert slides[0]["notes"] is not None
        assert "Speaker notes go here" in slides[0]["notes"]

    def test_directives_stripped(self, tmp_path: Path) -> None:
        md = "---\nmarp: true\n---\n\n<!-- _class: title -->\n# T\n"
        f = tmp_path / "dir.md"
        f.write_text(md, encoding="utf-8")
        slides = parse_marp(f)
        assert slides[0]["class"] == "title"
        # Directive should not appear in notes
        assert slides[0]["notes"] is None


# ------------------------------------------------------------------
# _convert_html_columns
# ------------------------------------------------------------------


class TestConvertHtmlColumns:
    def test_basic_conversion(self) -> None:
        html = '<div class="columns">\n<div>\nLeft\n</div>\n<div>\nRight\n</div>\n</div>'
        result = _convert_html_columns(html)
        assert "{.columns}" in result
        assert '{.column width="50%"}' in result
        assert "Left" in result
        assert "Right" in result

    def test_no_columns(self) -> None:
        text = "# Just text\n\nNo columns here."
        assert _convert_html_columns(text) == text

    def test_preserves_content(self) -> None:
        html = (
            '<div class="columns"><div>\n'
            "### Header\n| A | B |\n|---|---|\n| 1 | 2 |\n"
            "</div><div>\n"
            "Some text\n</div></div>"
        )
        result = _convert_html_columns(html)
        assert "### Header" in result
        assert "| A | B |" in result
        assert "Some text" in result


# ------------------------------------------------------------------
# slide_to_pandoc
# ------------------------------------------------------------------


class TestSlideToPandoc:
    def test_title_slide(self) -> None:
        slide = {
            "class": "title",
            "title": "Main Title",
            "body_lines": ["## Subtitle"],
            "bg_images": [],
            "inline_images": [],
            "notes": None,
        }
        md = slide_to_pandoc(slide)
        assert md.startswith("# Main Title")
        assert "## Subtitle" in md

    def test_content_slide(self) -> None:
        slide = {
            "class": None,
            "title": "Content",
            "body_lines": ["- A", "- B"],
            "bg_images": [],
            "inline_images": [],
            "notes": None,
        }
        md = slide_to_pandoc(slide)
        assert "# Content" in md
        assert "- A" in md

    def test_inline_image_with_text(self) -> None:
        slide = {
            "class": None,
            "title": "Diagram",
            "body_lines": ["Description here"],
            "bg_images": [],
            "inline_images": ["fig.png"],
            "notes": None,
        }
        md = slide_to_pandoc(slide)
        assert "{.columns}" in md
        assert "![](fig.png)" in md
        assert "Description here" in md

    def test_bg_image_columns(self) -> None:
        slide = {
            "class": None,
            "title": "Photo",
            "body_lines": ["Caption text"],
            "bg_images": [{"side": "right", "pct": 48, "path": "img.jpg"}],
            "inline_images": [],
            "notes": None,
        }
        md = slide_to_pandoc(slide)
        assert "{.columns}" in md
        assert "![](img.jpg)" in md

    def test_table_with_text_wrapped(self) -> None:
        slide = {
            "class": None,
            "title": "Data",
            "body_lines": [
                "Intro text.",
                "",
                "| A | B |",
                "|---|---|",
                "| 1 | 2 |",
            ],
            "bg_images": [],
            "inline_images": [],
            "notes": None,
        }
        md = slide_to_pandoc(slide)
        assert "{.columns}" in md
        assert "| A | B |" in md
        assert "Intro text." in md

    def test_already_columns_not_rewrapped(self) -> None:
        slide = {
            "class": None,
            "title": "Pre-wrapped",
            "body_lines": [
                ":::::::::::::: {.columns}",
                '::: {.column width="50%"}',
                "Left",
                ":::",
                '::: {.column width="50%"}',
                "Right",
                ":::",
                "::::::::::::::",
            ],
            "bg_images": [],
            "inline_images": [],
            "notes": None,
        }
        md = slide_to_pandoc(slide)
        # Should appear exactly once, not nested
        assert md.count("{.columns}") == 1

    def test_speaker_notes(self) -> None:
        slide = {
            "class": None,
            "title": "Noted",
            "body_lines": ["Body"],
            "bg_images": [],
            "inline_images": [],
            "notes": "Say this aloud",
        }
        md = slide_to_pandoc(slide)
        assert "::: notes" in md
        assert "Say this aloud" in md

    def test_image_only(self) -> None:
        slide = {
            "class": None,
            "title": "Full Image",
            "body_lines": [],
            "bg_images": [],
            "inline_images": ["chart.png"],
            "notes": None,
        }
        md = slide_to_pandoc(slide)
        assert "![](chart.png)" in md
        assert "{.columns}" not in md


# ------------------------------------------------------------------
# compose_editable_slides (integration, requires pandoc)
# ------------------------------------------------------------------


@pytest.mark.skipif(find_tool("pandoc") is None, reason="pandoc not installed")
class TestComposeEditableSlides:
    def test_marp_to_pptx(self, marp_file: Path, tmp_path: Path) -> None:
        from semos.agentura.document.composition._editable_slides import (
            compose_editable_slides,
        )

        out = tmp_path / "out.pptx"
        result = compose_editable_slides(marp_file, out)
        assert result == out
        assert out.exists()
        assert out.stat().st_size > 1000

    def test_pandoc_to_pptx(self, pandoc_file: Path, tmp_path: Path) -> None:
        from semos.agentura.document.composition._editable_slides import (
            compose_editable_slides,
        )

        out = tmp_path / "out.pptx"
        result = compose_editable_slides(pandoc_file, out)
        assert result == out
        assert out.exists()

    def test_marp_produces_pandoc_intermediate(self, marp_file: Path, tmp_path: Path) -> None:
        from semos.agentura.document.composition._editable_slides import (
            compose_editable_slides,
        )

        out = tmp_path / "out.pptx"
        compose_editable_slides(marp_file, out)
        pandoc_md = tmp_path / "out.pandoc.md"
        assert pandoc_md.exists()

    def test_pandoc_no_intermediate(self, pandoc_file: Path, tmp_path: Path) -> None:
        from semos.agentura.document.composition._editable_slides import (
            compose_editable_slides,
        )

        out = tmp_path / "out.pptx"
        compose_editable_slides(pandoc_file, out)
        # No .pandoc.md for direct pandoc input
        pandoc_md = tmp_path / "out.pandoc.md"
        assert not pandoc_md.exists()

    def test_custom_reference(self, pandoc_file: Path, tmp_path: Path) -> None:
        from semos.agentura.document.composition._editable_slides import (
            compose_editable_slides,
            default_reference,
        )

        out = tmp_path / "out.pptx"
        ref = default_reference()
        compose_editable_slides(pandoc_file, out, reference_doc=ref)
        assert out.exists()
