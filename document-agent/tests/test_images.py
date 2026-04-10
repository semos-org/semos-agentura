"""Tests for digestion/_images.py - annotation parsing and markdown assembly."""


from document_agent.digestion._images import (
    _build_alt_text,
    _clean_json_alt_text,
    _parse_annotation,
    combine_markdown,
    inline_images_as_base64,
    replace_images_in_markdown,
    save_images,
)
from document_agent.digestion._ocr_models import OCRResponse


class TestParseAnnotation:
    def test_valid_json(self):
        raw = '{"image_type": "photo", "description": "A sunset"}'
        result = _parse_annotation(raw)
        assert result["image_type"] == "photo"
        assert result["description"] == "A sunset"

    def test_dict_passthrough(self):
        d = {"image_type": "chart"}
        assert _parse_annotation(d) == d

    def test_malformed_json_partial_extract(self):
        raw = '{"image_type": "diagram", "description": "truncated...'
        result = _parse_annotation(raw)
        assert result["image_type"] == "diagram"

    def test_plain_string_fallback(self):
        result = _parse_annotation("just a string")
        assert result == {"description": "just a string"}


class TestBuildAltText:
    def test_full_annotation(self):
        ann = {"image_type": "chart", "description": "Bar chart", "text_content": "Sales Q1"}
        alt = _build_alt_text(ann)
        assert "chart" in alt
        assert "Bar chart" in alt
        assert "Sales Q1" in alt

    def test_empty_annotation(self):
        assert _build_alt_text({}) == ""

    def test_partial_annotation(self):
        assert "diagram" in _build_alt_text({"image_type": "diagram"})


class TestCleanJsonAltText:
    def test_json_alt_replaced(self):
        md = '![{"image_type": "photo", "description": "Cat"}](image.png)'
        result = _clean_json_alt_text(md)
        assert "photo" in result
        assert "Cat" in result
        assert "{" not in result.split("](")[0]

    def test_non_json_alt_unchanged(self):
        md = "![A nice photo](image.png)"
        assert _clean_json_alt_text(md) == md


class TestReplaceImagesInMarkdown:
    def test_replaces_image_refs(self):
        md = "# Title\n\n![img-0](img-0)\n\nText"
        image_map = {"img-0": "images/photo_001.png"}
        annotation_map = {"img-0": {"description": "A photo"}}
        result = replace_images_in_markdown(md, image_map, annotation_map)
        assert "images/photo_001.png" in result
        assert "A photo" in result
        assert "img-0" not in result.split("](")[0] or "A photo" in result


class TestSaveImages:
    def test_saves_base64_images(self, tmp_dir):
        import base64

        b64 = base64.b64encode(b"\x89PNG\r\n\x1a\nfakedata").decode()
        resp = OCRResponse({
            "pages": [{
                "index": 0,
                "markdown": "![img-0](img-0)",
                "images": [{"id": "img-0", "image_base64": b64, "image_annotation": '{"description":"test"}'}],
            }],
        })
        image_map, ann_map = save_images(resp, tmp_dir, "doc")
        assert "img-0" in image_map
        assert (tmp_dir / image_map["img-0"]).exists()
        assert ann_map["img-0"]["description"] == "test"


class TestCombineMarkdown:
    def test_combines_pages(self):
        resp = OCRResponse({
            "pages": [
                {"index": 0, "markdown": "# Page 1"},
                {"index": 1, "markdown": "# Page 2"},
            ],
        })
        result = combine_markdown(resp, {}, {})
        assert "Page 1" in result
        assert "Page 2" in result

    def test_resolves_tables_in_output(self):
        resp = OCRResponse({
            "pages": [{
                "index": 0,
                "markdown": "[tbl-0.md](tbl-0.md)",
                "tables": [{"id": "tbl-0.md", "content": "| A | B |"}],
            }],
        })
        result = combine_markdown(resp, {}, {})
        assert "| A | B |" in result
        assert "[tbl-" not in result


class TestInlineImagesBase64:
    def test_inlines_tables(self):
        resp = OCRResponse({
            "pages": [{
                "index": 0,
                "markdown": "Text\n[tbl-0.md](tbl-0.md)",
                "tables": [{"id": "tbl-0.md", "content": "| X |"}],
            }],
        })
        result = inline_images_as_base64(resp, {})
        assert "| X |" in result
        assert "[tbl-" not in result
