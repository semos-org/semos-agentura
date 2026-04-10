"""Shared fixtures for document-agent tests."""

from __future__ import annotations

import struct
import zlib
from pathlib import Path
from zipfile import ZipFile

import pytest


@pytest.fixture
def tmp_dir(tmp_path: Path) -> Path:
    return tmp_path


@pytest.fixture
def sample_png(tmp_path: Path) -> Path:
    """Create a minimal valid 1x1 red PNG file."""
    # Minimal PNG: 8-byte sig + IHDR + IDAT + IEND
    sig = b"\x89PNG\r\n\x1a\n"

    def _chunk(ctype: bytes, data: bytes) -> bytes:
        return (
            struct.pack(">I", len(data))
            + ctype
            + data
            + struct.pack(">I", zlib.crc32(ctype + data) & 0xFFFFFFFF)
        )

    ihdr = struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0)
    raw_row = b"\x00\xff\x00\x00"  # filter=None, R=255, G=0, B=0
    idat = zlib.compress(raw_row)

    data = sig + _chunk(b"IHDR", ihdr) + _chunk(b"IDAT", idat) + _chunk(b"IEND", b"")
    p = tmp_path / "test.png"
    p.write_bytes(data)
    return p


@pytest.fixture
def sample_drawio_xml() -> str:
    return (
        '<mxfile><diagram name="Page-1">'
        "<mxGraphModel><root>"
        '<mxCell id="0"/>'
        '<mxCell id="1" parent="0"/>'
        '<mxCell id="2" value="Hello" style="rounded=1;" vertex="1" parent="1">'
        '<mxGeometry x="100" y="100" width="120" height="60" as="geometry"/>'
        "</mxCell>"
        "</root></mxGraphModel>"
        "</diagram></mxfile>"
    )


@pytest.fixture
def sample_docx(tmp_path: Path) -> Path:
    """Create a minimal DOCX with a content control and a legacy field."""
    doc_xml = """\
<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"
            xmlns:w14="http://schemas.microsoft.com/office/word/2010/wordml">
<w:body>
  <!-- Content control: date picker -->
  <w:p>
    <w:sdt>
      <w:sdtPr>
        <w:alias w:val="StartDate"/>
        <w:date w:fullDate="2026-01-01T00:00:00Z">
          <w:dateFormat w:val="dd.MM.yyyy"/>
        </w:date>
      </w:sdtPr>
      <w:sdtContent>
        <w:p><w:r><w:t>01.01.2026</w:t></w:r></w:p>
      </w:sdtContent>
    </w:sdt>
  </w:p>

  <!-- Content control: checkbox -->
  <w:p>
    <w:sdt>
      <w:sdtPr>
        <w:alias w:val="Approved"/>
        <w14:checkbox>
          <w14:checked w14:val="0"/>
          <w14:checkedState w14:val="2611"/>
          <w14:uncheckedState w14:val="2610"/>
        </w14:checkbox>
      </w:sdtPr>
      <w:sdtContent>
        <w:p><w:r><w:t>\u2610</w:t></w:r></w:p>
      </w:sdtContent>
    </w:sdt>
  </w:p>

  <!-- Legacy text field -->
  <w:p>
    <w:r>
      <w:fldChar w:fldCharType="begin">
        <w:ffData>
          <w:name w:val="FullName"/>
          <w:textInput>
            <w:default w:val=""/>
          </w:textInput>
        </w:ffData>
      </w:fldChar>
    </w:r>
    <w:r><w:instrText> FORMTEXT </w:instrText></w:r>
    <w:r><w:fldChar w:fldCharType="separate"/></w:r>
    <w:r><w:t>placeholder</w:t></w:r>
    <w:r><w:fldChar w:fldCharType="end"/></w:r>
  </w:p>

  <!-- Table with label + empty cell -->
  <w:tbl>
    <w:tr>
      <w:tc><w:p><w:r><w:t>Name:</w:t></w:r></w:p></w:tc>
      <w:tc><w:p><w:r><w:t></w:t></w:r></w:p></w:tc>
    </w:tr>
    <w:tr>
      <w:tc><w:p><w:r><w:t>City:</w:t></w:r></w:p></w:tc>
      <w:tc><w:p><w:r><w:t></w:t></w:r></w:p></w:tc>
    </w:tr>
  </w:tbl>
</w:body>
</w:document>"""

    docx_path = tmp_path / "test_form.docx"
    with ZipFile(docx_path, "w") as z:
        z.writestr("[Content_Types].xml", """\
<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
  <Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>
  <Default Extension="xml" ContentType="application/xml"/>
  <Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>
</Types>""")
        z.writestr("_rels/.rels", """\
<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
  <Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="word/document.xml"/>
</Relationships>""")
        z.writestr("word/_rels/document.xml.rels", """\
<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"/>""")
        z.writestr("word/document.xml", doc_xml)

    return docx_path


def _make_png_bytes() -> bytes:
    """Create minimal 1x1 red PNG bytes."""
    sig = b"\x89PNG\r\n\x1a\n"

    def _chunk(ctype: bytes, data: bytes) -> bytes:
        return (
            struct.pack(">I", len(data))
            + ctype
            + data
            + struct.pack(">I", zlib.crc32(ctype + data) & 0xFFFFFFFF)
        )

    ihdr = struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0)
    raw_row = b"\x00\xff\x00\x00"
    idat = zlib.compress(raw_row)
    return sig + _chunk(b"IHDR", ihdr) + _chunk(b"IDAT", idat) + _chunk(b"IEND", b"")


# Shared DOCX boilerplate
_CONTENT_TYPES_RICH = """\
<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
  <Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>
  <Default Extension="xml" ContentType="application/xml"/>
  <Default Extension="png" ContentType="image/png"/>
  <Override PartName="/word/document.xml"
    ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>
  <Override PartName="/word/footnotes.xml"
    ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.footnotes+xml"/>
  <Override PartName="/word/comments.xml"
    ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.comments+xml"/>
</Types>"""

_RELS_ROOT = """\
<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
  <Relationship Id="rId1"
    Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument"
    Target="word/document.xml"/>
</Relationships>"""

_RELS_DOC_RICH = """\
<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
  <Relationship Id="rId1"
    Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/footnotes"
    Target="footnotes.xml"/>
  <Relationship Id="rId2"
    Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/comments"
    Target="comments.xml"/>
  <Relationship Id="rId3"
    Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/image"
    Target="media/image1.png"/>
</Relationships>"""


@pytest.fixture
def sample_docx_rich(tmp_path: Path) -> Path:
    """DOCX with a heading, footnote, tracked changes, comment, image, and table."""
    _w = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
    _r = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
    _wp = "http://schemas.openxmlformats.org/drawingml/2006/wordprocessingDrawing"
    _a = "http://schemas.openxmlformats.org/drawingml/2006/main"
    _pic = "http://schemas.openxmlformats.org/drawingml/2006/picture"

    doc_xml = f"""\
<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<w:document xmlns:w="{_w}" xmlns:r="{_r}"
            xmlns:wp="{_wp}" xmlns:a="{_a}" xmlns:pic="{_pic}">
<w:body>

  <!-- Heading -->
  <w:p>
    <w:pPr><w:pStyle w:val="Heading1"/></w:pPr>
    <w:r><w:t>1. Introduction</w:t></w:r>
  </w:p>

  <!-- Normal paragraph with footnote reference -->
  <w:p>
    <w:r><w:t>This is the first paragraph with a footnote</w:t></w:r>
    <w:r>
      <w:rPr><w:rStyle w:val="FootnoteReference"/></w:rPr>
      <w:footnoteReference w:id="1"/>
    </w:r>
    <w:r><w:t>.</w:t></w:r>
  </w:p>

  <!-- Tracked insertion (must be inside w:p) -->
  <w:p>
    <w:ins w:id="10" w:author="Alice" w:date="2026-03-01T10:00:00Z">
      <w:r><w:t>This text was added by Alice.</w:t></w:r>
    </w:ins>
  </w:p>

  <!-- Tracked deletion (must be inside w:p) -->
  <w:p>
    <w:del w:id="11" w:author="Bob" w:date="2026-03-02T14:00:00Z">
      <w:r><w:delText>This text was removed by Bob.</w:delText></w:r>
    </w:del>
  </w:p>

  <!-- Comment -->
  <w:p>
    <w:commentRangeStart w:id="1"/>
    <w:r><w:t>This paragraph has a comment.</w:t></w:r>
    <w:commentRangeEnd w:id="1"/>
    <w:r>
      <w:rPr><w:rStyle w:val="CommentReference"/></w:rPr>
      <w:commentReference w:id="1"/>
    </w:r>
  </w:p>

  <!-- Image -->
  <w:p>
    <w:r>
      <w:drawing>
        <wp:inline>
          <a:graphic>
            <a:graphicData uri="http://schemas.openxmlformats.org/drawingml/2006/picture">
              <pic:pic>
                <pic:blipFill>
                  <a:blip r:embed="rId3"/>
                </pic:blipFill>
              </pic:pic>
            </a:graphicData>
          </a:graphic>
        </wp:inline>
      </w:drawing>
    </w:r>
  </w:p>

  <!-- Table with properties and grid -->
  <w:tbl>
    <w:tblPr>
      <w:tblStyle w:val="TableGrid"/>
      <w:tblW w:w="0" w:type="auto"/>
    </w:tblPr>
    <w:tblGrid>
      <w:gridCol w:w="4000"/>
      <w:gridCol w:w="4000"/>
    </w:tblGrid>
    <w:tr>
      <w:tc><w:p><w:r><w:t>Column A</w:t></w:r></w:p></w:tc>
      <w:tc><w:p><w:r><w:t>Column B</w:t></w:r></w:p></w:tc>
    </w:tr>
    <w:tr>
      <w:tc><w:p><w:r><w:t>Value 1</w:t></w:r></w:p></w:tc>
      <w:tc><w:p><w:r><w:t>Value 2</w:t></w:r></w:p></w:tc>
    </w:tr>
  </w:tbl>

</w:body>
</w:document>"""

    footnotes_xml = f"""\
<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<w:footnotes xmlns:w="{_w}">
  <w:footnote w:type="separator" w:id="-1">
    <w:p><w:r><w:separator/></w:r></w:p>
  </w:footnote>
  <w:footnote w:id="1">
    <w:p>
      <w:r>
        <w:rPr><w:rStyle w:val="FootnoteReference"/></w:rPr>
        <w:footnoteRef/>
      </w:r>
      <w:r><w:t> This is a footnote.</w:t></w:r>
    </w:p>
  </w:footnote>
</w:footnotes>"""

    comments_xml = f"""\
<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<w:comments xmlns:w="{_w}">
  <w:comment w:id="1" w:author="Reviewer" w:date="2026-03-05T09:00:00Z">
    <w:p><w:r><w:t>Please clarify this section.</w:t></w:r></w:p>
  </w:comment>
</w:comments>"""

    docx_path = tmp_path / "test_rich.docx"
    with ZipFile(docx_path, "w") as z:
        z.writestr("[Content_Types].xml", _CONTENT_TYPES_RICH)
        z.writestr("_rels/.rels", _RELS_ROOT)
        z.writestr("word/_rels/document.xml.rels", _RELS_DOC_RICH)
        z.writestr("word/document.xml", doc_xml)
        z.writestr("word/footnotes.xml", footnotes_xml)
        z.writestr("word/comments.xml", comments_xml)
        z.writestr("word/media/image1.png", _make_png_bytes())

    return docx_path


@pytest.fixture
def sample_reference_docx(tmp_path: Path) -> Path:
    """Minimal DOCX with custom styles for reference-doc testing."""
    _w = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"

    styles_xml = f"""\
<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<w:styles xmlns:w="{_w}">
  <w:docDefaults>
    <w:rPrDefault>
      <w:rPr>
        <w:rFonts w:ascii="Arial" w:hAnsi="Arial"/>
        <w:sz w:val="24"/>
      </w:rPr>
    </w:rPrDefault>
  </w:docDefaults>
  <w:style w:type="paragraph" w:styleId="Heading1">
    <w:name w:val="heading 1"/>
    <w:pPr><w:outlineLvl w:val="0"/></w:pPr>
    <w:rPr>
      <w:rFonts w:ascii="Arial" w:hAnsi="Arial"/>
      <w:b/>
      <w:sz w:val="32"/>
    </w:rPr>
  </w:style>
  <w:style w:type="paragraph" w:styleId="Normal">
    <w:name w:val="Normal"/>
    <w:rPr>
      <w:rFonts w:ascii="Arial" w:hAnsi="Arial"/>
      <w:sz w:val="24"/>
    </w:rPr>
  </w:style>
</w:styles>"""

    doc_xml = f"""\
<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<w:document xmlns:w="{_w}">
<w:body>
  <w:p>
    <w:pPr><w:pStyle w:val="Heading1"/></w:pPr>
    <w:r><w:t>Template Heading</w:t></w:r>
  </w:p>
  <w:p><w:r><w:t>Template body text.</w:t></w:r></w:p>
</w:body>
</w:document>"""

    content_types = """\
<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
  <Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>
  <Default Extension="xml" ContentType="application/xml"/>
  <Override PartName="/word/document.xml"
    ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>
  <Override PartName="/word/styles.xml"
    ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.styles+xml"/>
</Types>"""

    rels_doc = """\
<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
  <Relationship Id="rId1"
    Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles"
    Target="styles.xml"/>
</Relationships>"""

    docx_path = tmp_path / "reference.docx"
    with ZipFile(docx_path, "w") as z:
        z.writestr("[Content_Types].xml", content_types)
        z.writestr("_rels/.rels", _RELS_ROOT)
        z.writestr("word/_rels/document.xml.rels", rels_doc)
        z.writestr("word/document.xml", doc_xml)
        z.writestr("word/styles.xml", styles_xml)

    return docx_path
