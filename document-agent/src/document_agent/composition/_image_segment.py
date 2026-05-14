"""Image segmentation helpers for cut mode."""

from __future__ import annotations

import io
import logging

from PIL import Image

logger = logging.getLogger(__name__)


def remove_background(image_bytes: bytes) -> bytes:
    """Remove background using rembg. Returns PNG with alpha channel.

    Raises ImportError if rembg is not installed.
    """
    import rembg

    result = rembg.remove(image_bytes)
    return result


def crop_to_content(image_bytes: bytes, padding: int = 10) -> bytes:
    """Crop transparent PNG to its content bounding box.

    Returns the cropped PNG bytes. If the image is fully
    transparent, returns the original unchanged.
    """
    img = Image.open(io.BytesIO(image_bytes)).convert("RGBA")
    alpha = img.getchannel("A")
    bbox = alpha.getbbox()
    if bbox is None:
        return image_bytes

    x0, y0, x1, y1 = bbox
    x0 = max(0, x0 - padding)
    y0 = max(0, y0 - padding)
    x1 = min(img.width, x1 + padding)
    y1 = min(img.height, y1 + padding)

    cropped = img.crop((x0, y0, x1, y1))
    buf = io.BytesIO()
    cropped.save(buf, format="PNG")
    return buf.getvalue()


def extract_element(image_bytes: bytes, description: str | None = None) -> bytes:
    """Extract element from image: remove background + crop to content.

    Falls back to just crop_to_content if rembg is not available.
    """
    try:
        no_bg = remove_background(image_bytes)
    except (ImportError, ModuleNotFoundError):
        logger.warning("rembg not installed, skipping background removal")
        no_bg = image_bytes
    return crop_to_content(no_bg)
