"""PDF splitting and OCR response merging."""

from __future__ import annotations

import logging
import tempfile
from pathlib import Path

from pypdf import PdfReader, PdfWriter

from ._ocr_models import OCRResponse

logger = logging.getLogger(__name__)


def split_pdf(file_path: Path, max_pages: int = 10) -> list[Path]:
    """Split a PDF into chunks of at most *max_pages* pages.

    Returns a list of temporary file paths. Caller must delete them after use.
    Returns empty list if no split is needed.
    """
    reader = PdfReader(file_path)
    total = len(reader.pages)
    if total <= max_pages:
        return []

    chunks: list[Path] = []
    for start in range(0, total, max_pages):
        writer = PdfWriter()
        for i in range(start, min(start + max_pages, total)):
            writer.add_page(reader.pages[i])
        tmp = tempfile.NamedTemporaryFile(suffix=".pdf", delete=False, prefix=f"{file_path.stem}_part")
        writer.write(tmp)
        tmp.close()
        chunks.append(Path(tmp.name))

    return chunks


def merge_ocr_responses(responses: list[OCRResponse]) -> OCRResponse:
    """Merge multiple OCR responses into one, adjusting page indices.

    Image IDs are prefixed with ``chunk{n}_`` so that IDs from different
    chunks don't collide.
    """
    merged: dict = {"pages": [], "document_annotation": None}
    page_offset = 0
    for chunk_idx, resp in enumerate(responses):
        for page in resp.pages:
            md = page.markdown
            images = []
            for img in page.images:
                old_id = img.id
                new_id = f"chunk{chunk_idx}_{old_id}"
                md = md.replace(old_id, new_id)
                images.append({
                    "id": new_id,
                    "image_base64": img.image_base64,
                    "image_annotation": img.image_annotation,
                })
            merged["pages"].append({
                "index": page_offset + page.index,
                "markdown": md,
                "images": images,
            })
        page_offset += len(resp.pages)
        if resp.document_annotation is not None:
            merged["document_annotation"] = resp.document_annotation
    return OCRResponse(merged)
