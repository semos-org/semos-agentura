"""Tests for _constants.py - file type sets and MIME map."""

from document_agent._constants import (
    IMAGE_EXTENSIONS,
    MIME_MAP,
    OFFICE_EXTENSIONS,
    PDF_EXTENSIONS,
    SUPPORTED_EXTENSIONS,
)


class TestExtensionSets:
    def test_pdf_extensions(self):
        assert ".pdf" in PDF_EXTENSIONS

    def test_image_extensions(self):
        for ext in (".png", ".jpg", ".jpeg", ".webp"):
            assert ext in IMAGE_EXTENSIONS

    def test_office_extensions(self):
        for ext in (".docx", ".pptx", ".xlsx", ".odt"):
            assert ext in OFFICE_EXTENSIONS

    def test_supported_is_union(self):
        assert SUPPORTED_EXTENSIONS == PDF_EXTENSIONS | IMAGE_EXTENSIONS | OFFICE_EXTENSIONS

    def test_no_overlap_pdf_image(self):
        assert PDF_EXTENSIONS.isdisjoint(IMAGE_EXTENSIONS)


class TestMimeMap:
    def test_known_types(self):
        assert MIME_MAP["png"] == "image/png"
        assert MIME_MAP["jpg"] == "image/jpeg"
        assert MIME_MAP["jpeg"] == "image/jpeg"
        assert MIME_MAP["webp"] == "image/webp"
