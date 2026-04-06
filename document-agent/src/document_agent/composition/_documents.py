"""Document generation via Pandoc CLI."""

from __future__ import annotations

import logging
import subprocess
from pathlib import Path

from ..exceptions import CompositionError
from ..models import OutputFormat

logger = logging.getLogger(__name__)

_FORMAT_MAP = {
    OutputFormat.PDF: "pdf",
    OutputFormat.DOCX: "docx",
    OutputFormat.ODT: "odt",
    OutputFormat.HTML: "html",
}


def compose_document(
    md_path: Path,
    output_path: Path,
    format: OutputFormat,
    *,
    pandoc_path: Path,
) -> Path:
    """Generate a document from markdown using pandoc."""
    output_path = output_path.resolve()
    output_path.parent.mkdir(parents=True, exist_ok=True)
    pandoc_format = _FORMAT_MAP.get(format)
    if not pandoc_format:
        raise CompositionError(f"Unsupported document format: {format}")

    cmd = [
        str(pandoc_path),
        str(md_path),
        "-o",
        str(output_path),
        "--standalone",
    ]

    # For PDF, pandoc needs a PDF engine
    if format == OutputFormat.PDF:
        cmd.extend(["--pdf-engine=xelatex"])

    logger.info("Running: %s", " ".join(cmd))
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=120, cwd=md_path.parent)
    if result.returncode != 0:
        raise CompositionError(f"Pandoc failed: {result.stderr}")

    if not output_path.exists():
        raise CompositionError(f"Pandoc did not produce output: {output_path}")
    logger.info("Document written: %s", output_path)
    return output_path
