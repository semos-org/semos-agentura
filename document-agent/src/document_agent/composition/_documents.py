"""Document generation via Pandoc CLI."""

from __future__ import annotations

import logging
import subprocess
from pathlib import Path
from zipfile import ZipFile

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
    reference_doc: Path | None = None,
) -> Path:
    """Generate a document from markdown using pandoc.

    Args:
        md_path: Path to the Markdown source file.
        output_path: Where to write the output document.
        format: Target format (PDF, DOCX, ODT, HTML).
        pandoc_path: Path to pandoc binary.
        reference_doc: Optional DOCX/ODT file whose styles (fonts, sizes,
            margins, headers/footers) are applied to the output.
    """
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

    # Apply styles from a reference document (DOCX/ODT only)
    if reference_doc and format in (OutputFormat.DOCX, OutputFormat.ODT):
        cmd.extend(["--reference-doc", str(reference_doc)])

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


def autofit_tables(docx_path: Path) -> None:
    """Change table layout from fixed to autofit in a DOCX file.

    Pandoc generates fixed-width equal columns by default. This
    post-processes the DOCX to let Word auto-size columns to content.
    """
    tmp = docx_path.with_suffix(".tmp")
    with ZipFile(docx_path, "r") as zin, ZipFile(tmp, "w") as zout:
        for item in zin.infolist():
            data = zin.read(item.filename)
            if item.filename == "word/document.xml":
                text = data.decode("utf-8")
                text = text.replace(
                    '<w:tblLayout w:type="fixed"',
                    '<w:tblLayout w:type="autofit"',
                )
                data = text.encode("utf-8")
            zout.writestr(item, data)
    tmp.replace(docx_path)
