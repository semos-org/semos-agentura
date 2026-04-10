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

# Common draw.io desktop install paths (checked as fallback for image rendering)
_DRAWIO_DESKTOP_PATHS = [
    Path(r"C:\Program Files\draw.io\draw.io.exe"),
    Path(r"C:\Program Files (x86)\draw.io\draw.io.exe"),
    Path.home() / "AppData" / "Local" / "Programs" / "draw.io" / "draw.io.exe",
    Path.home() / "scoop" / "apps" / "draw.io" / "current" / "draw.io.exe",
    Path("/usr/bin/drawio"),
    Path("/usr/local/bin/drawio"),
    Path("/Applications/draw.io.app/Contents/MacOS/draw.io"),
]


def _find_drawio_desktop(env_override: str | None = None) -> Path | None:
    """Find the draw.io desktop app (not the npm CLI wrapper).

    The desktop app can render inline images correctly, unlike the
    npm drawio-export CLI which uses a limited headless Electron.
    Set DRAWIO_DESKTOP_PATH in .env or settings to specify the path.
    """
    import shutil

    # Check explicit setting or env var
    for candidate in [env_override, os.environ.get("DRAWIO_DESKTOP_PATH")]:
        if candidate:
            p = Path(candidate)
            if p.is_file():
                return p

    # Check common install paths
    for p in _DRAWIO_DESKTOP_PATHS:
        if p.is_file():
            return p

    # Check system PATH (draw.io desktop vs drawio npm CLI)
    for name in ["draw.io", "drawio-desktop"]:
        found = shutil.which(name)
        if found:
            return Path(found)

    return None


_DRAWIO_BLOCK_RE = re.compile(r"```drawio\s*\n(.*?)```", re.DOTALL)


def find_drawio_blocks(markdown: str) -> list[tuple[int, int, str]]:
    """Find ```drawio fenced code blocks.

    Returns list of (start, end, xml_code) tuples.
    """
    return [(m.start(), m.end(), m.group(1).strip()) for m in _DRAWIO_BLOCK_RE.finditer(markdown)]


def _compress_diagram_content(inner_xml: str, url_encode: bool = False) -> str:
    """Compress mxGraphModel XML the way draw.io does.

    Without url_encode: deflate => base64 (for PNG tEXt embedding).
    With url_encode: URL-encode => deflate => base64 (for .drawio files
    and CLI rendering).
    """
    from urllib.parse import quote

    data = inner_xml
    if url_encode:
        data = quote(data, safe="")
    raw_deflate = zlib.compress(data.encode("utf-8"))[2:-4]
    return base64.b64encode(raw_deflate).decode()


def _build_mxfile_for_png(xml: str, url_encode: bool = False) -> str:
    """Build compressed mxfile XML.

    Used for PNG tEXt embedding (url_encode=False) and for CLI rendering
    (url_encode=True). The input xml may have <mxGraphModel> as a direct
    child of <diagram> (uncompressed) - we compress it.
    """
    from lxml import etree

    root = etree.fromstring(xml.encode("utf-8"))
    for diagram in root.iter("diagram"):
        children = list(diagram)
        if children:
            inner = etree.tostring(children[0], encoding="unicode")
            for child in children:
                diagram.remove(child)
            diagram.text = _compress_diagram_content(inner, url_encode=url_encode)

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
    from urllib.parse import unquote

    from lxml import etree

    root = etree.fromstring(mxfile_xml.encode("utf-8"))
    for diagram in root.iter("diagram"):
        if diagram.text and not list(diagram):
            raw = _decompress_diagram_content(diagram.text.strip())
            # draw.io URL-encodes the decompressed XML
            inner_xml = unquote(raw)
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
    from urllib.parse import unquote

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
                # draw.io URL-encodes the entire mxfile XML in PNG metadata
                if not raw.startswith("<"):
                    raw = unquote(raw)
                return _parse_mxfile_from_png(raw)
        offset += 12 + clen
    return None


_INLINE_IMAGE_RE = re.compile(r"(data:image/[^,]+,)([A-Za-z0-9+/=\s]{100,})")


def _externalize_images(xml: str, work_dir: Path) -> tuple[str, list[Path]]:
    """Replace inline base64 images with external file paths for rendering.

    The draw.io CLI Electron renderer can't display inline data URI images
    in headless mode. Saving them as temp files and referencing by path
    makes them renderable.

    Returns (modified_xml, list_of_temp_files).
    """
    temp_files: list[Path] = []
    counter = 0

    def _replace(m: re.Match) -> str:
        nonlocal counter
        counter += 1
        prefix = m.group(1)
        b64_data = m.group(2).replace("\n", "").replace(" ", "")
        ext = "png"
        if "jpeg" in prefix or "jpg" in prefix:
            ext = "jpg"
        img_path = work_dir / f"_img_{counter}.{ext}"
        img_path.write_bytes(base64.b64decode(b64_data))
        temp_files.append(img_path)
        return str(img_path).replace("\\", "/")

    modified = _INLINE_IMAGE_RE.sub(_replace, xml)
    if counter:
        logger.info("Externalized %d images for CLI rendering", counter)
    return modified, temp_files


def render_drawio_to_png(
    xml: str,
    output_path: Path,
    *,
    drawio_path: Path,
    drawio_desktop_path: str | None = None,
) -> Path:
    """Render a draw.io diagram to PNG.

    Uses the npm CLI by default. If the XML contains inline base64
    images, automatically switches to the draw.io desktop app (which
    supports image rendering). Set DRAWIO_DESKTOP_PATH in .env.
    The original XML (with inline images) is embedded in PNG metadata.
    """
    import shutil as _shutil

    has_images = _INLINE_IMAGE_RE.search(xml) is not None
    render_exe = drawio_path

    # Prefer desktop app for diagrams with embedded images
    if has_images:
        desktop = _find_drawio_desktop(drawio_desktop_path)
        if desktop:
            render_exe = desktop
            logger.info("Using draw.io desktop for image rendering: %s", desktop)
        else:
            logger.warning(
                "Diagram has embedded images but draw.io desktop not found. "
                "Set DRAWIO_DESKTOP_PATH in .env for correct image rendering."
            )

    work_dir = Path(tempfile.mkdtemp(prefix="drawio_render_"))
    input_path = work_dir / "diagram.drawio"
    input_path.write_text(xml, encoding="utf-8")

    try:
        cmd = [str(render_exe), "--export", "--format", "png", "--output", str(output_path), str(input_path)]
        # shell=True needed for .cmd wrappers on Windows, not for .exe
        use_shell = os.name == "nt" and str(render_exe).endswith(".cmd")
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=120, shell=use_shell)
        if result.returncode != 0:
            raise DrawioRenderError(
                f"drawio export failed (exit {result.returncode}):\n"
                f"stderr: {result.stderr.strip()}\n"
                f"stdout: {result.stdout.strip()}"
            )
        if not output_path.exists():
            raise DrawioRenderError(
                f"drawio produced no output file at {output_path}\n"
                f"stderr: {result.stderr.strip()}\n"
                f"stdout: {result.stdout.strip()}"
            )
        # Embed the original XML (with inline images) in PNG metadata
        _embed_xml_in_png(output_path, xml)
        return output_path
    finally:
        _shutil.rmtree(work_dir, ignore_errors=True)


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
