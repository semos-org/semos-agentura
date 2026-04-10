"""Extract diagram source from various input formats."""

from __future__ import annotations

import base64
import logging
import re
import tempfile
from dataclasses import dataclass
from pathlib import Path

from .._llm_client import LLMClient
from ..config import Settings

logger = logging.getLogger(__name__)

_MERMAID_KEYWORDS = re.compile(
    r"^\s*(?:graph|flowchart|sequenceDiagram|classDiagram|"
    r"stateDiagram|erDiagram|gantt|pie|gitGraph|journey|"
    r"mindmap|timeline|quadrantChart|sankey|xychart|block)\b",
    re.MULTILINE,
)

_DRAWIO_PREFIX = re.compile(r"^\s*<(?:mxfile|mxGraphModel)\b")

_IMAGE_EXTENSIONS = {
    ".png",
    ".jpg",
    ".jpeg",
    ".webp",
    ".tiff",
    ".tif",
    ".bmp",
    ".gif",
}

_ANALYSIS_PROMPT = (
    "Analyze this diagram image in detail. Describe:\n"
    "1. The type of diagram (flowchart, sequence, network, etc.)\n"
    "2. All elements/nodes with their labels\n"
    "3. All connections/arrows between elements with labels\n"
    "4. The overall layout and flow direction\n"
    "5. Any grouping, coloring, or styling\n"
    "Be precise and exhaustive - this will be used to recreate "
    "the diagram in code."
)


@dataclass
class DiagramSource:
    """Extracted diagram source for use in the optimization loop."""

    code: str | None = None
    image_b64: str | None = None
    diagram_type: str = "unknown"
    description: str | None = None
    # Stripped embedded images store (cells + uris for restore)
    embedded_images: dict | None = None


# Matches both "data:image/png;base64,..." and "data:image/png,..." (draw.io shorthand)
_B64_IMAGE_RE = re.compile(r"(data:image/[^,]+,)([A-Za-z0-9+/=\s]{100,})")


def strip_embedded_images(xml: str) -> tuple[str, dict[str, str]]:
    """Strip base64-encoded images from draw.io XML.

    Replaces image data URIs with short placeholders in mxCell style
    attributes. Also stores the full original mxCell XML elements so
    they can be re-injected even if the LLM drops them entirely.

    Returns (stripped_xml, image_store) where image_store contains:
      - "cells": {cell_id: full_original_mxCell_xml} for re-injection
      - "uris": {placeholder_id: original_data_uri} for inline restore
    """
    from lxml import etree

    store: dict[str, str] = {}

    try:
        root = etree.fromstring(xml.encode("utf-8"))
    except etree.XMLSyntaxError:
        return xml, store

    cells_store: dict[str, str] = {}
    uri_store: dict[str, str] = {}
    counter = 0

    for cell in root.iter("mxCell"):
        style = cell.get("style", "")
        if not _B64_IMAGE_RE.search(style):
            continue
        counter += 1
        cell_id = cell.get("id", f"_img_{counter}")
        pid = f"__IMG_{counter}__"

        # Save the full original cell XML
        cells_store[cell_id] = etree.tostring(cell, encoding="unicode")

        # Replace data URI in style with short placeholder
        new_style = _B64_IMAGE_RE.sub(pid, style)
        cell.set("style", new_style)
        uri_store[pid] = _B64_IMAGE_RE.search(style).group(0)

    if not counter:
        return xml, store

    stripped = etree.tostring(root, encoding="unicode", xml_declaration=False)
    store = {"cells": cells_store, "uris": uri_store}
    logger.info(
        "Stripped %d image cells from draw.io XML (%d -> %d chars)",
        counter,
        len(xml),
        len(stripped),
    )
    return stripped, store


def restore_embedded_images(xml: str, store: dict[str, str]) -> str:
    """Re-insert images into draw.io XML.

    Two strategies:
    1. If the LLM kept the placeholders, replace them with original URIs.
    2. If the LLM dropped image cells entirely, re-inject the original
       mxCell elements into <root>.
    """
    if not store:
        return xml
    uris = store.get("uris", {})
    cells = store.get("cells", {})

    # Strategy 1: replace inline placeholders
    for pid, uri in uris.items():
        xml = xml.replace(pid, uri)

    # Strategy 2: re-inject missing image cells
    if cells:
        from lxml import etree

        try:
            root = etree.fromstring(xml.encode("utf-8"))
        except etree.XMLSyntaxError:
            return xml

        # Find all current cell IDs
        existing_ids = {c.get("id") for c in root.iter("mxCell")}

        # Find the <root> element to append to
        root_el = root.find(".//root")
        if root_el is None:
            return xml

        injected = 0
        for cell_id, cell_xml in cells.items():
            if cell_id not in existing_ids:
                cell_el = etree.fromstring(cell_xml.encode("utf-8"))
                root_el.append(cell_el)
                injected += 1

        if injected:
            logger.info("Re-injected %d missing image cells", injected)
            xml = etree.tostring(root, encoding="unicode", xml_declaration=False)

    return xml


def _decompress_and_strip_drawio(xml: str) -> tuple[str, dict[str, str]]:
    """Decompress draw.io XML (if compressed) and strip embedded images.

    Draw.io stores diagram content as deflate+base64 inside <diagram>
    elements. This decompresses to get the actual mxGraphModel XML,
    strips base64 images, and returns readable XML the LLM can edit.
    """
    from urllib.parse import unquote

    from lxml import etree

    from ._drawio import _decompress_diagram_content

    try:
        root = etree.fromstring(xml.encode("utf-8"))
    except etree.XMLSyntaxError:
        return strip_embedded_images(xml)

    # Decompress <diagram> elements that have compressed text content
    decompressed = False
    for diagram in root.iter("diagram"):
        if diagram.text and not list(diagram):
            try:
                raw = _decompress_diagram_content(diagram.text.strip())
                # draw.io URL-encodes the decompressed XML
                inner_xml = unquote(raw)
                inner_el = etree.fromstring(inner_xml.encode("utf-8"))
                diagram.text = None
                diagram.append(inner_el)
                decompressed = True
            except Exception:
                pass

    if decompressed:
        xml = etree.tostring(root, encoding="unicode", xml_declaration=False)

    # Now strip base64 images from the decompressed XML
    stripped, images = strip_embedded_images(xml)
    return stripped, images


def _detect_type_from_string(text: str) -> str:
    """Detect diagram type from inline code content."""
    if _DRAWIO_PREFIX.search(text):
        return "drawio"
    if _MERMAID_KEYWORDS.search(text):
        return "mermaid"
    return "unknown"


def _detect_type_from_path(path: Path) -> str:
    """Detect diagram type from file extension."""
    name = path.name.lower()
    if name.endswith(".drawio.png"):
        return "drawio"
    if name.endswith(".drawio") or name.endswith(".drawio.xml"):
        return "drawio"
    if name.endswith(".mmd") or name.endswith(".mermaid"):
        return "mermaid"
    if name.endswith(".svg"):
        return "unknown"  # needs content inspection
    if path.suffix.lower() in _IMAGE_EXTENSIONS:
        return "unknown"  # needs VLM analysis
    return "unknown"


def _read_image_as_b64(path: Path) -> str:
    """Read an image file and return as base64 string."""
    return base64.b64encode(path.read_bytes()).decode()


async def extract_diagram_source(
    source: Path | str,
    *,
    codegen_client: LLMClient | None = None,
    settings: Settings | None = None,
) -> DiagramSource:
    """Extract diagram code and/or image from various inputs.

    Args:
        source: File path or inline diagram code string.
        codegen_client: LLM client for VLM analysis of images.
        settings: Settings for tool paths.
    """
    if settings is None:
        settings = Settings()

    # --- Inline code string ---
    if isinstance(source, str) and not Path(source).exists():
        dtype = _detect_type_from_string(source)
        return DiagramSource(
            code=source,
            diagram_type=dtype,
        )

    # --- File path ---
    path = Path(source) if isinstance(source, str) else source
    if not path.exists():
        raise FileNotFoundError(f"Source not found: {path}")

    name = path.name.lower()

    # .mmd / .mermaid - raw mermaid code
    if name.endswith((".mmd", ".mermaid")):
        code = path.read_text(encoding="utf-8")
        return DiagramSource(
            code=code,
            diagram_type="mermaid",
        )

    # .drawio / .drawio.xml - raw drawio XML
    if name.endswith(".drawio") or name.endswith(".drawio.xml"):
        code = path.read_text(encoding="utf-8")
        code, images = _decompress_and_strip_drawio(code)
        return DiagramSource(
            code=code,
            diagram_type="drawio",
            embedded_images=images or None,
        )

    # .drawio.png - extract embedded XML
    if name.endswith(".drawio.png"):
        from ._drawio import extract_xml_from_png

        xml = extract_xml_from_png(path)
        if xml:
            xml, images = _decompress_and_strip_drawio(xml)
            return DiagramSource(
                code=xml,
                image_b64=_read_image_as_b64(path),
                diagram_type="drawio",
                embedded_images=images or None,
            )
        # No embedded XML - fall through to image analysis
        logger.warning(
            "No embedded XML in %s, treating as image",
            path,
        )

    # .svg - check for embedded diagram code
    if name.endswith(".svg"):
        return await _extract_from_svg(path, codegen_client)

    # Any image - VLM analysis
    if path.suffix.lower() in _IMAGE_EXTENSIONS or name.endswith(".drawio.png"):
        return await _extract_from_image(path, codegen_client)

    raise ValueError(f"Unsupported source format: {path.suffix}")


async def _extract_from_svg(
    path: Path,
    codegen_client: LLMClient | None,
) -> DiagramSource:
    """Extract diagram from SVG: check embedded content or analyze."""
    svg_text = path.read_text(encoding="utf-8")

    # Check for embedded drawio XML
    if "<mxfile" in svg_text or "<mxGraphModel" in svg_text:
        return DiagramSource(
            code=svg_text,
            diagram_type="drawio",
        )

    # Convert SVG to PNG for analysis
    png_b64 = _svg_to_png_b64(path)
    if png_b64 and codegen_client:
        analysis = await codegen_client.chat_with_image(
            [{"role": "user", "content": _ANALYSIS_PROMPT}],
            png_b64,
        )
        return DiagramSource(
            image_b64=png_b64,
            diagram_type="unknown",
            description=analysis,
        )

    return DiagramSource(
        image_b64=png_b64,
        diagram_type="unknown",
    )


def _svg_to_png_b64(svg_path: Path) -> str | None:
    """Convert SVG to PNG using available tools, return base64."""
    import shutil
    import subprocess

    for tool in ("rsvg-convert", "inkscape"):
        exe = shutil.which(tool)
        if not exe:
            continue
        with tempfile.NamedTemporaryFile(
            suffix=".png",
            delete=False,
        ) as f:
            tmp = Path(f.name)
        try:
            if "inkscape" in tool:
                cmd = [
                    exe,
                    str(svg_path),
                    "--export-type=png",
                    f"--export-filename={tmp}",
                ]
            else:
                cmd = [exe, str(svg_path), "-o", str(tmp)]
            subprocess.run(
                cmd,
                capture_output=True,
                timeout=30,
            )
            if tmp.exists() and tmp.stat().st_size > 0:
                return base64.b64encode(
                    tmp.read_bytes(),
                ).decode()
        finally:
            tmp.unlink(missing_ok=True)

    logger.warning("No SVG converter found (rsvg-convert, inkscape)")
    return None


async def _extract_from_image(
    path: Path,
    codegen_client: LLMClient | None,
) -> DiagramSource:
    """Analyze a raster image (photo, screenshot) via VLM."""
    image_b64 = _read_image_as_b64(path)

    if codegen_client:
        analysis = await codegen_client.chat_with_image(
            [{"role": "user", "content": _ANALYSIS_PROMPT}],
            image_b64,
        )
        return DiagramSource(
            image_b64=image_b64,
            diagram_type="unknown",
            description=analysis,
        )

    return DiagramSource(
        image_b64=image_b64,
        diagram_type="unknown",
    )
