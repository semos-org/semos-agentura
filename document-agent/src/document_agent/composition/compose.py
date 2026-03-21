"""Main entry point for document composition (Markdown to documents)."""

from __future__ import annotations

import logging
import shutil
import tempfile
from pathlib import Path

from .._utils import require_tool
from ..config import Settings
from ..models import ComposeResult, OutputFormat
from ._documents import compose_document
from ._markdown_prep import prepare_markdown_file
from ._drawio import replace_drawio_blocks
from ._mermaid import replace_mermaid_blocks
from ._slides import compose_slides
from .._utils import find_tool

logger = logging.getLogger(__name__)


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
                logger.warning(
                    "drawio not found, skipping drawio rendering"
                )

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
