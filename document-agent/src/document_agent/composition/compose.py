"""Main entry point for document composition (Markdown to documents)."""

from __future__ import annotations

import logging
import shutil
import tempfile
from pathlib import Path

from .._utils import find_tool, require_tool
from ..config import Settings
from ..models import ComposeResult, OutputFormat
from ._documents import compose_document
from ._drawio import replace_drawio_blocks
from ._markdown_prep import prepare_markdown_file
from ._mermaid import replace_mermaid_blocks
from ._slides import compose_slides

logger = logging.getLogger(__name__)

_IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".gif", ".svg", ".webp", ".bmp"}
_IMG_REF = __import__("re").compile(r"!\[[^\]]*\]\(([^)]+)\)")


def _copy_referenced_images(
    md_path: Path,
    source: Path | str,
    work_dir: Path,
) -> None:
    """Copy image files referenced in markdown to work_dir.

    Checks the source file's directory and common output locations
    for images referenced as ![alt](filename.png).
    """
    md_text = md_path.read_text(encoding="utf-8")
    source_path = Path(source) if isinstance(source, (str, Path)) else None
    search_dirs: list[Path] = []
    if source_path and source_path.is_file():
        search_dirs.append(source_path.parent)
    if source_path and source_path.is_dir():
        search_dirs.append(source_path)
    # Also check common agent output directories
    for parent in [work_dir.parent, Path("output/document-agent")]:
        if parent.is_dir() and parent not in search_dirs:
            search_dirs.append(parent)

    for match in _IMG_REF.finditer(md_text):
        ref = match.group(1)
        # Skip URLs and data URIs
        if ref.startswith(("http://", "https://", "data:")):
            continue
        ref_path = Path(ref)
        # Skip if already in work_dir
        if (work_dir / ref_path.name).exists():
            continue
        # Search for the file
        for d in search_dirs:
            candidate = d / ref_path.name
            if candidate.exists():
                dest = work_dir / ref_path.name
                shutil.copy2(candidate, dest)
                # Update markdown reference if path differs
                if ref != ref_path.name:
                    md_text = md_text.replace(ref, ref_path.name)
                logger.info("Copied image %s to work_dir", ref_path.name)
                break

    md_path.write_text(md_text, encoding="utf-8")


def compose(
    source: Path | str,
    output_path: Path | str,
    format: OutputFormat,
    *,
    is_slides: bool = False,
    render_mermaid: bool = True,
    render_drawio: bool = True,
    settings: Settings | None = None,
) -> ComposeResult:
    """Compose a document from Markdown.

    Args:
        source: Path to .md file, or markdown string (with optional base64 images).
        output_path: Where to write the output file.
        format: Target format (PDF, DOCX, ODT, PPTX, HTML).
        is_slides: If True, use Marp for slide generation. If False, use Pandoc.
        render_mermaid: Pre-render mermaid code blocks to images before conversion.
        settings: Settings instance (auto-created from env if None).

    Returns:
        ComposeResult with output path.
    """
    if settings is None:
        settings = Settings()
    output_path = Path(output_path)

    # Create a temporary work directory
    work_dir = Path(tempfile.mkdtemp(prefix="doc_agent_compose_"))

    try:
        # Prepare markdown: write to file, extract base64 images
        md_path = prepare_markdown_file(source, work_dir)

        # Copy referenced images from source directory to work_dir.
        # Handles images materialized by A2A file transfer or
        # produced by prior tool calls in the same agent.
        _copy_referenced_images(md_path, source, work_dir)

        # Render mermaid diagrams if requested
        if render_mermaid:
            mmdc = find_tool("mmdc", settings.mmdc_path)
            if mmdc:
                md_text = md_path.read_text(encoding="utf-8")
                md_text = replace_mermaid_blocks(
                    md_text,
                    output_dir=work_dir,
                    inline=False,
                    mmdc_path=mmdc,
                )
                md_path.write_text(md_text, encoding="utf-8")
            else:
                logger.warning("mmdc not found, skipping mermaid rendering")

        # Render drawio diagrams if requested
        if render_drawio:
            drawio = find_tool("drawio", settings.drawio_path)
            if drawio:
                md_text = md_path.read_text(encoding="utf-8")
                md_text = replace_drawio_blocks(
                    md_text,
                    output_dir=work_dir,
                    inline=False,
                    drawio_path=drawio,
                )
                md_path.write_text(md_text, encoding="utf-8")
            else:
                logger.warning("drawio not found, skipping drawio rendering")

        # Route to the right composer
        if is_slides:
            marp = require_tool("marp", settings.marp_path)
            compose_slides(md_path, output_path, format, marp_path=marp, libre_office_path=settings.libre_office_path)
        else:
            pandoc = require_tool("pandoc", settings.pandoc_path)
            compose_document(md_path, output_path, format, pandoc_path=pandoc)

        return ComposeResult(output_path=output_path, format=format)

    finally:
        shutil.rmtree(work_dir, ignore_errors=True)
