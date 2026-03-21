"""Convert Office documents (DOCX, PPTX, XLSX, ODT, etc.) to PDF via LibreOffice."""

from __future__ import annotations

import logging
import subprocess
import tempfile
from pathlib import Path

from .._utils import find_tool
from ..exceptions import ConversionError, ToolNotFoundError

logger = logging.getLogger(__name__)

_SOFFICE_NAMES = ["soffice", "libreoffice"]

_COMMON_PATHS = [
    Path(r"C:\Program Files\LibreOffice\program\soffice.exe"),
    Path(r"C:\Program Files (x86)\LibreOffice\program\soffice.exe"),
    Path("/usr/bin/soffice"),
    Path("/usr/bin/libreoffice"),
    Path("/Applications/LibreOffice.app/Contents/MacOS/soffice"),
]


def _find_libreoffice(env_override: str | None = None) -> Path:
    """Find LibreOffice (soffice) executable."""
    if env_override:
        p = Path(env_override)
        if p.is_file():
            return p
    for name in _SOFFICE_NAMES:
        found = find_tool(name)
        if found:
            return found
    for p in _COMMON_PATHS:
        if p.is_file():
            return p
    raise ToolNotFoundError(
        "LibreOffice (soffice) not found on PATH. "
        "Install LibreOffice or set LIBRE_OFFICE_PATH in settings."
    )


def convert_office_to_pdf(file_path: Path, libre_office_path: str | None = None) -> Path:
    """Convert an Office document to PDF using LibreOffice.

    Returns the path to the generated PDF file (in a temp directory).
    """
    soffice = _find_libreoffice(libre_office_path)
    out_dir = tempfile.mkdtemp(prefix="doc_agent_")

    cmd = [
        str(soffice),
        "--headless",
        "--convert-to", "pdf",
        "--outdir", out_dir,
        str(file_path),
    ]
    logger.info("Converting %s to PDF via LibreOffice...", file_path.name)
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
    if result.returncode != 0:
        raise ConversionError(f"LibreOffice conversion failed: {result.stderr}")

    pdf_path = Path(out_dir) / f"{file_path.stem}.pdf"
    if not pdf_path.exists():
        raise ConversionError(f"Expected PDF not found after conversion: {pdf_path}")
    return pdf_path
