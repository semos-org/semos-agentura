"""Render PDF/Office document pages to PNG images for VLM inspection.

Uses pypdfium2 (Apache/BSD licensed) for PDF rendering. Office documents
are converted to PDF first via LibreOffice (see digestion._office).
"""

from __future__ import annotations

import base64
import io
import logging
from pathlib import Path

from ._constants import OFFICE_EXTENSIONS, PDF_EXTENSIONS

logger = logging.getLogger(__name__)

# Minimum rendered page width so field text stays legible for the VLM.
_MIN_PAGE_WIDTH = 1500


def render_document_pages(
    path: Path,
    out_dir: Path,
    *,
    dpi: int = 150,
    max_pages: int | None = None,
    libre_office_path: str | None = None,
) -> list[Path]:
    """Render each page of a PDF or Office document to a PNG file.

    Args:
        path: Source PDF, DOCX, ODT, etc.
        out_dir: Directory to write the page PNGs into.
        dpi: Render resolution (72 = 1:1 PDF points).
        max_pages: Cap the number of pages rendered (None = all). When the
            document has more pages, the extra pages are skipped and a
            warning is logged (no silent truncation).
        libre_office_path: Override for the LibreOffice binary (Office input).

    Returns:
        List of PNG paths, one per rendered page, in page order.
    """
    ext = path.suffix.lower()
    pdf_path = path
    temp_pdf: Path | None = None

    if ext in OFFICE_EXTENSIONS:
        from .digestion._office import convert_office_to_pdf

        temp_pdf = convert_office_to_pdf(path, libre_office_path)
        pdf_path = temp_pdf
    elif ext not in PDF_EXTENSIONS:
        raise ValueError(f"Cannot render pages for unsupported type: {ext}")

    try:
        return _render_pdf_pages(pdf_path, out_dir, stem=path.stem, dpi=dpi, max_pages=max_pages)
    finally:
        if temp_pdf is not None:
            temp_pdf.unlink(missing_ok=True)


def _render_pdf_pages(
    pdf_path: Path,
    out_dir: Path,
    *,
    stem: str,
    dpi: int,
    max_pages: int | None,
) -> list[Path]:
    """Render PDF pages to PNGs using pypdfium2."""
    return [m["path"] for m in render_pdf_pages_meta(pdf_path, out_dir, stem=stem, dpi=dpi, max_pages=max_pages)]


def render_pdf_pages_meta(
    pdf_path: Path,
    out_dir: Path,
    *,
    stem: str = "page",
    dpi: int = 150,
    max_pages: int | None = None,
) -> list[dict]:
    """Render PDF pages and return per-page geometry metadata.

    Each dict has: path, page (0-based index), width_pt, height_pt, scale
    (pixels per PDF point). The scale lets callers map PDF field rects
    (points, bottom-left origin) to image pixels (top-left origin).
    """
    import pypdfium2 as pdfium

    out_dir.mkdir(parents=True, exist_ok=True)
    base_scale = dpi / 72.0

    pdf = pdfium.PdfDocument(str(pdf_path))
    # Initialize the form environment so AcroForm field values (e.g. text
    # filled via pypdf) are drawn into the rendered page. No-op for PDFs
    # without forms.
    try:
        pdf.init_forms()
    except Exception:
        logger.debug("init_forms() failed or no forms present", exc_info=True)
    try:
        n_pages = len(pdf)
        limit = n_pages if max_pages is None else min(n_pages, max_pages)
        if limit < n_pages:
            logger.warning("Rendering first %d of %d pages (max_pages=%s)", limit, n_pages, max_pages)

        meta: list[dict] = []
        for i in range(limit):
            page = pdf[i]
            width_pt, height_pt = page.get_size()
            # Bump scale so the page is at least _MIN_PAGE_WIDTH px wide.
            page_scale = base_scale
            if width_pt * page_scale < _MIN_PAGE_WIDTH:
                page_scale = _MIN_PAGE_WIDTH / width_pt
            image = page.render(scale=page_scale).to_pil()
            dest = out_dir / f"{stem}_p{i + 1:02d}.png"
            image.save(dest)
            meta.append(
                {
                    "path": dest,
                    "page": i,
                    "width_pt": width_pt,
                    "height_pt": height_pt,
                    "scale": page_scale,
                }
            )
        return meta
    finally:
        pdf.close()


def downscale_image_b64(raw_b64: str, target_b64_len: int) -> str:
    """Downscale a PNG/JPEG so its base64 fits within target_b64_len.

    Uses progressive JPEG quality reduction, then resize. Returns the
    base64 string (without data URI prefix). If Pillow is unavailable,
    truncates as a last resort.
    """
    if len(raw_b64) <= target_b64_len:
        return raw_b64

    try:
        from PIL import Image
    except ImportError:
        logger.warning("Pillow not installed, cannot downscale image")
        return raw_b64[:target_b64_len]

    raw_bytes = base64.b64decode(raw_b64)
    img = Image.open(io.BytesIO(raw_bytes))

    for quality in (75, 60, 45, 30):
        buf = io.BytesIO()
        rgb = img.convert("RGB") if img.mode != "RGB" else img
        rgb.save(buf, format="JPEG", quality=quality, optimize=True)
        b64 = base64.b64encode(buf.getvalue()).decode()
        if len(b64) <= target_b64_len:
            logger.info("Downscaled page image: quality=%d, %d -> %d chars", quality, len(raw_b64), len(b64))
            return b64

    for scale in (0.75, 0.5, 0.4):
        w, h = int(img.width * scale), int(img.height * scale)
        if w < _MIN_PAGE_WIDTH and scale < 0.75:
            break
        resized = img.resize((w, h), Image.LANCZOS)
        buf = io.BytesIO()
        resized.convert("RGB").save(buf, format="JPEG", quality=45, optimize=True)
        b64 = base64.b64encode(buf.getvalue()).decode()
        if len(b64) <= target_b64_len:
            logger.info("Downscaled page image: scale=%.0f%%, %d -> %d chars", scale * 100, len(raw_b64), len(b64))
            return b64

    logger.warning("Could not downscale image to target size, using smallest version")
    return b64
