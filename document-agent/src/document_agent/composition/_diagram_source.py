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
    ".png", ".jpg", ".jpeg", ".webp",
    ".tiff", ".tif", ".bmp", ".gif",
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
        return DiagramSource(
            code=code,
            diagram_type="drawio",
        )

    # .drawio.png - extract embedded XML
    if name.endswith(".drawio.png"):
        from ._drawio import extract_xml_from_png

        xml = extract_xml_from_png(path)
        if xml:
            return DiagramSource(
                code=xml,
                image_b64=_read_image_as_b64(path),
                diagram_type="drawio",
            )
        # No embedded XML - fall through to image analysis
        logger.warning(
            "No embedded XML in %s, treating as image", path,
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
            suffix=".png", delete=False,
        ) as f:
            tmp = Path(f.name)
        try:
            if "inkscape" in tool:
                cmd = [
                    exe, str(svg_path),
                    "--export-type=png",
                    f"--export-filename={tmp}",
                ]
            else:
                cmd = [exe, str(svg_path), "-o", str(tmp)]
            subprocess.run(
                cmd, capture_output=True, timeout=30,
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
