"""Tests for digestion/_pdf.py - PDF splitting and OCR response merging."""

from pathlib import Path

from document_agent.digestion._ocr_models import OCRResponse
from document_agent.digestion._pdf import merge_ocr_responses, split_pdf
from pypdf import PdfWriter


def _create_pdf(path: Path, num_pages: int) -> Path:
    """Create a minimal PDF with a given number of blank pages."""
    writer = PdfWriter()
    for _ in range(num_pages):
        writer.add_blank_page(width=72, height=72)
    with open(path, "wb") as f:
        writer.write(f)
    return path


class TestSplitPdf:
    def test_no_split_needed(self, tmp_dir):
        pdf = _create_pdf(tmp_dir / "small.pdf", 5)
        chunks = split_pdf(pdf, max_pages=10)
        assert chunks == []

    def test_exact_split(self, tmp_dir):
        pdf = _create_pdf(tmp_dir / "exact.pdf", 10)
        chunks = split_pdf(pdf, max_pages=5)
        try:
            assert len(chunks) == 2
            for c in chunks:
                assert c.exists()
        finally:
            for c in chunks:
                c.unlink(missing_ok=True)

    def test_uneven_split(self, tmp_dir):
        pdf = _create_pdf(tmp_dir / "uneven.pdf", 7)
        chunks = split_pdf(pdf, max_pages=3)
        try:
            assert len(chunks) == 3  # 3+3+1
        finally:
            for c in chunks:
                c.unlink(missing_ok=True)

    def test_single_page(self, tmp_dir):
        pdf = _create_pdf(tmp_dir / "one.pdf", 1)
        assert split_pdf(pdf, max_pages=10) == []


class TestMergeOCRResponses:
    def test_merges_pages(self):
        r1 = OCRResponse({"pages": [{"index": 0, "markdown": "page0"}]})
        r2 = OCRResponse({"pages": [{"index": 0, "markdown": "page1"}]})
        merged = merge_ocr_responses([r1, r2])
        assert len(merged.pages) == 2
        assert merged.pages[0].index == 0
        assert merged.pages[1].index == 1

    def test_image_id_collision_avoided(self):
        r1 = OCRResponse({
            "pages": [{
                "index": 0,
                "markdown": "![img-0](img-0)",
                "images": [{"id": "img-0", "image_base64": "a"}],
            }],
        })
        r2 = OCRResponse({
            "pages": [{
                "index": 0,
                "markdown": "![img-0](img-0)",
                "images": [{"id": "img-0", "image_base64": "b"}],
            }],
        })
        merged = merge_ocr_responses([r1, r2])
        ids = [img.id for p in merged.pages for img in p.images]
        assert len(set(ids)) == 2  # no duplicates
        assert "chunk0_img-0" in ids
        assert "chunk1_img-0" in ids

    def test_document_annotation_last_wins(self):
        r1 = OCRResponse({"pages": [], "document_annotation": {"a": 1}})
        r2 = OCRResponse({"pages": [], "document_annotation": {"b": 2}})
        merged = merge_ocr_responses([r1, r2])
        assert merged.document_annotation == {"b": 2}

    def test_empty_list(self):
        merged = merge_ocr_responses([])
        assert len(merged.pages) == 0
