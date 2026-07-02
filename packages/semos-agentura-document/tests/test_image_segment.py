"""Tests for image segmentation (cut mode helpers)."""

from __future__ import annotations

import io

from PIL import Image
from semos.agentura.document.composition._image_segment import crop_to_content


def _make_transparent_png_with_square(
    canvas_w: int = 200,
    canvas_h: int = 200,
    square_x: int = 50,
    square_y: int = 60,
    square_size: int = 40,
) -> bytes:
    """Create a transparent PNG with a single colored square."""
    img = Image.new("RGBA", (canvas_w, canvas_h), (0, 0, 0, 0))
    for y in range(square_y, square_y + square_size):
        for x in range(square_x, square_x + square_size):
            img.putpixel((x, y), (255, 0, 0, 255))
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


class TestCropToContent:
    def test_crops_to_content_bounds(self):
        png = _make_transparent_png_with_square(
            canvas_w=200,
            canvas_h=200,
            square_x=50,
            square_y=60,
            square_size=40,
        )
        cropped = crop_to_content(png, padding=0)
        img = Image.open(io.BytesIO(cropped))
        assert img.width == 40
        assert img.height == 40

    def test_adds_padding(self):
        png = _make_transparent_png_with_square(
            canvas_w=200,
            canvas_h=200,
            square_x=50,
            square_y=60,
            square_size=40,
        )
        cropped = crop_to_content(png, padding=10)
        img = Image.open(io.BytesIO(cropped))
        assert img.width == 60
        assert img.height == 60

    def test_fully_transparent_returns_original(self):
        img = Image.new("RGBA", (100, 100), (0, 0, 0, 0))
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        png = buf.getvalue()
        result = crop_to_content(png)
        result_img = Image.open(io.BytesIO(result))
        assert result_img.width == 100
        assert result_img.height == 100


class TestExtractElement:
    def test_extract_element_crops_after_bg_removal(self):
        from unittest.mock import patch

        png = _make_transparent_png_with_square(
            canvas_w=200,
            canvas_h=200,
            square_x=80,
            square_y=80,
            square_size=30,
        )
        with patch(
            "semos.agentura.document.composition._image_segment.remove_background",
            return_value=png,
        ):
            from semos.agentura.document.composition._image_segment import extract_element

            result = extract_element(b"any-input")
            img = Image.open(io.BytesIO(result))
            assert img.width <= 60
            assert img.height <= 60
