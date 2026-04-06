"""Extract and render draw.io diagrams from Markdown."""

from __future__ import annotations

import base64
import logging
import os
import re
import struct
import subprocess
import tempfile
import zlib
from pathlib import Path

from ..exceptions import DrawioRenderError

logger = logging.getLogger(__name__)

_DRAWIO_BLOCK_RE = re.compile(r"```drawio\s*\n(.*?)```", re.DOTALL)


def find_drawio_blocks(markdown: str) -> list[tuple[int, int, str]]:
    """Find ```drawio fenced code blocks.

    Returns list of (start, end, xml_code) tuples.
    """
    return [(m.start(), m.end(), m.group(1).strip()) for m in _DRAWIO_BLOCK_RE.finditer(markdown)]


def _compress_diagram_content(inner_xml: str) -> str:
    """Compress mxGraphModel XML the way draw.io does:
    deflate => base64 (no URL-encoding for PNG embedding)."""
    raw_deflate = zlib.compress(inner_xml.encode("utf-8"))[2:-4]
    return base64.b64encode(raw_deflate).decode()


def _build_mxfile_for_png(xml: str) -> str:
    """Build the mxfile XML to embed in PNG.

    draw.io expects the tEXt chunk to contain raw mxfile XML where
    each <diagram> element's content is deflate+base64+urlencode
    compressed. The input xml may have <mxGraphModel> as a direct
    child of <diagram> (uncompressed) - we compress it.
    """
    from lxml import etree

    root = etree.fromstring(xml.encode("utf-8"))
    # Find all <diagram> elements and compress their child content
    for diagram in root.iter("diagram"):
        # Get the inner mxGraphModel XML
        children = list(diagram)
        if children:
            inner = etree.tostring(
                children[0],
                encoding="unicode",
            )
            # Remove child elements, set compressed text
            for child in children:
                diagram.remove(child)
            diagram.text = _compress_diagram_content(inner)

    return etree.tostring(root, encoding="unicode", xml_declaration=False)


def _embed_xml_in_png(png_path: Path, xml: str) -> None:
    """Embed draw.io XML into PNG as a tEXt chunk (mxfile key).

    This makes the PNG re-openable and editable in draw.io desktop.
    The tEXt chunk contains raw mxfile XML with compressed diagram
    content (matching draw.io's native PNG export format).
    """
    data = bytearray(png_path.read_bytes())
    # PNG structure: 8-byte signature, then chunks.
    # Insert tEXt chunk before the first IDAT chunk.
    keyword = b"mxfile"
    text_data = _build_mxfile_for_png(xml).encode("utf-8")
    chunk_data = keyword + b"\x00" + text_data
    chunk_len = struct.pack(">I", len(chunk_data))
    chunk_type = b"tEXt"
    chunk_crc = struct.pack(">I", zlib.crc32(chunk_type + chunk_data) & 0xFFFFFFFF)
    text_chunk = chunk_len + chunk_type + chunk_data + chunk_crc

    # Find first IDAT offset (after 8-byte signature)
    offset = 8
    while offset < len(data):
        clen = struct.unpack(">I", data[offset : offset + 4])[0]
        ctype = data[offset + 4 : offset + 8]
        if ctype == b"IDAT":
            break
        offset += 12 + clen  # 4 len + 4 type + data + 4 crc
    else:
        return  # no IDAT found, skip embedding

    data[offset:offset] = text_chunk
    png_path.write_bytes(bytes(data))


def _decompress_diagram_content(encoded: str) -> str:
    """Reverse of _compress_diagram_content: base64 => inflate => XML."""
    compressed = base64.b64decode(encoded)
    return (
        zlib.decompressobj(
            -zlib.MAX_WBITS,
        )
        .decompress(compressed)
        .decode("utf-8")
    )


def _parse_mxfile_from_png(mxfile_xml: str) -> str:
    """Reverse of _build_mxfile_for_png: decompress <diagram> text
    content back to inline <mxGraphModel> children."""
    from lxml import etree

    root = etree.fromstring(mxfile_xml.encode("utf-8"))
    for diagram in root.iter("diagram"):
        if diagram.text and not list(diagram):
            inner_xml = _decompress_diagram_content(
                diagram.text.strip(),
            )
            inner_el = etree.fromstring(inner_xml.encode("utf-8"))
            diagram.text = None
            diagram.append(inner_el)
    return etree.tostring(
        root,
        encoding="unicode",
        xml_declaration=False,
    )


def extract_xml_from_png(png_path: Path) -> str | None:
    """Reverse of _embed_xml_in_png: walk PNG chunks, find tEXt
    with keyword 'mxfile', return full decompressed drawio XML."""
    data = png_path.read_bytes()
    offset = 8  # skip PNG signature
    while offset < len(data):
        clen = struct.unpack(">I", data[offset : offset + 4])[0]
        ctype = data[offset + 4 : offset + 8]
        if ctype == b"tEXt":
            chunk_data = data[offset + 8 : offset + 8 + clen]
            nul = chunk_data.index(0)
            keyword = chunk_data[:nul]
            if keyword == b"mxfile":
                raw = chunk_data[nul + 1 :].decode("utf-8")
                return _parse_mxfile_from_png(raw)
        offset += 12 + clen
    return None


def render_drawio_to_png(xml: str, output_path: Path, *, drawio_path: Path) -> Path:
    """Render a draw.io diagram to PNG using the drawio CLI.

    The source XML is embedded in the PNG metadata so the file
    can be re-opened and edited in draw.io desktop.
    """
    with tempfile.NamedTemporaryFile(suffix=".drawio", mode="w", delete=False, encoding="utf-8") as f:
        f.write(xml)
        input_path = Path(f.name)

    try:
        cmd = [str(drawio_path), "-x", "-f", "png", "-o", str(output_path), str(input_path)]
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=120, shell=(os.name == "nt"))
        if result.returncode != 0:
            raise DrawioRenderError(f"drawio export failed: {result.stderr}")
        if not output_path.exists():
            raise DrawioRenderError(f"drawio did not produce output: {output_path}")
        _embed_xml_in_png(output_path, xml)
        return output_path
    finally:
        input_path.unlink(missing_ok=True)


def render_drawio_to_base64(xml: str, *, drawio_path: Path) -> str:
    """Render a draw.io diagram to PNG and return as base64 data URI."""
    with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as f:
        output_path = Path(f.name)

    try:
        render_drawio_to_png(xml, output_path, drawio_path=drawio_path)
        png_bytes = output_path.read_bytes()
        b64 = base64.b64encode(png_bytes).decode()
        return f"data:image/png;base64,{b64}"
    finally:
        output_path.unlink(missing_ok=True)


def replace_drawio_blocks(
    markdown: str,
    *,
    output_dir: Path | None = None,
    inline: bool = False,
    drawio_path: Path,
) -> str:
    """Replace all ```drawio blocks with rendered PNG images.

    If inline=True, images are base64-inlined. Otherwise saved to output_dir.
    """
    blocks = find_drawio_blocks(markdown)
    if not blocks:
        return markdown

    for i, (start, end, xml) in enumerate(reversed(blocks)):
        idx = len(blocks) - 1 - i
        logger.info("Rendering drawio diagram %d/%d...", idx + 1, len(blocks))
        try:
            if inline:
                data_uri = render_drawio_to_base64(xml, drawio_path=drawio_path)
                replacement = f"![drawio diagram]({data_uri})"
            else:
                if output_dir is None:
                    raise DrawioRenderError("output_dir required for file-based drawio rendering")
                output_dir.mkdir(parents=True, exist_ok=True)
                img_name = f"drawio_{idx + 1:03d}.drawio.png"
                img_path = output_dir / img_name
                render_drawio_to_png(xml, img_path, drawio_path=drawio_path)
                replacement = f"![]({img_name})"
            markdown = markdown[:start] + replacement + markdown[end:]
        except DrawioRenderError:
            logger.warning("Failed to render drawio diagram %d, keeping as code block", idx + 1)

    return markdown
