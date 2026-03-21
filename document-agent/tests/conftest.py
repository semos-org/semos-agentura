"""Shared fixtures for document-agent tests."""

from __future__ import annotations

import struct
import tempfile
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
