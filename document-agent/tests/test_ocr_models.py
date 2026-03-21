"""Tests for digestion/_ocr_models.py - OCR response parsing and table resolution."""

from document_agent.digestion._ocr_models import (
    OCRImage,
    OCRPage,
    OCRResponse,
    OCRTable,
)


class TestOCRTable:
    def test_basic(self):
        t = OCRTable({"id": "tbl-0.md", "content": "| A | B |", "format": "markdown"})
        assert t.id == "tbl-0.md"
        assert t.content == "| A | B |"
        assert t.format == "markdown"

    def test_defaults(self):
        t = OCRTable({})
        assert t.id == ""
        assert t.content == ""
        assert t.format == "markdown"


class TestOCRImage:
    def test_fields(self):
        img = OCRImage({"id": "img-1", "image_base64": "abc", "image_annotation": '{"x":1}'})
        assert img.id == "img-1"
        assert img.image_base64 == "abc"
        assert img.image_annotation == '{"x":1}'

    def test_optional_fields(self):
        img = OCRImage({"id": "img-2"})
        assert img.image_base64 is None
        assert img.image_annotation is None


class TestOCRPageResolveTables:
    def test_no_tables_returns_markdown(self):
        page = OCRPage({"markdown": "# Hello\nWorld"})
        assert page.resolve_tables() == "# Hello\nWorld"

    def test_single_table_resolved(self):
        md = "Before\n\n[tbl-0.md](tbl-0.md)\n\nAfter"
        page = OCRPage({
            "markdown": md,
            "tables": [{"id": "tbl-0.md", "content": "| A | B |\n|---|---|\n| 1 | 2 |"}],
        })
        result = page.resolve_tables()
        assert "[tbl-0.md]" not in result
        assert "| A | B |" in result
        assert "Before" in result
        assert "After" in result

    def test_multiple_tables_resolved(self):
        md = "[tbl-0.md](tbl-0.md)\n\n[tbl-1.md](tbl-1.md)"
        page = OCRPage({
            "markdown": md,
            "tables": [
                {"id": "tbl-0.md", "content": "Table 0"},
                {"id": "tbl-1.md", "content": "Table 1"},
            ],
        })
        result = page.resolve_tables()
        assert "Table 0" in result
        assert "Table 1" in result
        assert "[tbl-" not in result

    def test_unknown_ref_preserved(self):
        md = "[tbl-99.md](tbl-99.md)"
        page = OCRPage({"markdown": md, "tables": []})
        assert page.resolve_tables() == md

    def test_html_format_refs(self):
        md = "[tbl-0.html](tbl-0.html)"
        page = OCRPage({
            "markdown": md,
            "tables": [{"id": "tbl-0.html", "content": "<table><tr><td>X</td></tr></table>"}],
        })
        assert "<table>" in page.resolve_tables()

    def test_image_refs_not_affected(self):
        md = "![img-0](img-0)\n[tbl-0.md](tbl-0.md)"
        page = OCRPage({
            "markdown": md,
            "tables": [{"id": "tbl-0.md", "content": "| A |"}],
            "images": [{"id": "img-0", "image_base64": "abc"}],
        })
        result = page.resolve_tables()
        assert "![img-0](img-0)" in result
        assert "| A |" in result


class TestOCRResponseFromDict:
    def test_basic_dict(self):
        resp = OCRResponse({
            "pages": [
                {
                    "index": 0,
                    "markdown": "Hello",
                    "images": [{"id": "i1", "image_base64": "x"}],
                    "tables": [{"id": "t1", "content": "table"}],
                }
            ],
            "document_annotation": {"key": "value"},
        })
        assert len(resp.pages) == 1
        assert resp.pages[0].markdown == "Hello"
        assert len(resp.pages[0].images) == 1
        assert len(resp.pages[0].tables) == 1
        assert resp.document_annotation == {"key": "value"}

    def test_empty_dict(self):
        resp = OCRResponse({"pages": []})
        assert len(resp.pages) == 0
        assert resp.document_annotation is None


class TestOCRResponseFromSDKObject:
    def test_sdk_object_with_tables(self):
        class MockTable:
            id = "tbl-0.md"
            content = "| col |"
            format = "markdown"

        class MockImage:
            id = "img-0"
            image_base64 = "b64data"
            image_annotation = '{"type":"photo"}'

        class MockPage:
            index = 0
            markdown = "text [tbl-0.md](tbl-0.md)"
            images = [MockImage()]
            tables = [MockTable()]

        class MockSDKResponse:
            pages = [MockPage()]
            document_annotation = None

        resp = OCRResponse(MockSDKResponse())
        assert len(resp.pages) == 1
        assert len(resp.pages[0].tables) == 1
        assert resp.pages[0].tables[0].content == "| col |"
        assert "| col |" in resp.pages[0].resolve_tables()

    def test_sdk_object_without_tables(self):
        class MockPage:
            index = 0
            markdown = "hello"
            images = []

        class MockSDKResponse:
            pages = [MockPage()]
            document_annotation = None

        resp = OCRResponse(MockSDKResponse())
        assert len(resp.pages) == 1
        assert resp.pages[0].tables == []
