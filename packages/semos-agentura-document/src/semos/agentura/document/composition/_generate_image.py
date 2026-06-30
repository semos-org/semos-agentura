"""Standalone raster image generation, editing, and element extraction."""

from __future__ import annotations

import base64
import io
import logging
import uuid
from pathlib import Path
from typing import Literal

from PIL import Image

from .._image_client import ImageClient
from .._llm_client import LLMClient
from ..config import Settings
from ..exceptions import ProviderError
from ..models import ImageResult

logger = logging.getLogger(__name__)

_CUT_VLM_PROMPT = (
    "Look at this image and identify all distinct visual elements. "
    "For the element described as: {description} - "
    "return its bounding box. If multiple matches, return all of them. "
    "Coordinates are in pixels from the top-left corner."
)

_BBOX_SCHEMA: dict = {
    "type": "object",
    "properties": {
        "elements": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "label": {"type": "string", "description": "Short label for the element"},
                    "x": {"type": "integer", "description": "Left edge in pixels"},
                    "y": {"type": "integer", "description": "Top edge in pixels"},
                    "width": {"type": "integer", "description": "Width in pixels"},
                    "height": {"type": "integer", "description": "Height in pixels"},
                },
                "required": ["label", "x", "y", "width", "height"],
            },
        },
    },
    "required": ["elements"],
}


def _build_image_client(settings: Settings) -> ImageClient:
    ep = settings.image_gen_endpoint
    key = settings.image_gen_api_key
    model = settings.image_gen_model
    if not ep or not key:
        raise ProviderError("Image generation not configured. Set IMAGE_GEN_ENDPOINT and IMAGE_GEN_API_KEY.")
    return ImageClient(ep, key, model)


def _build_vlm_client(settings: Settings) -> LLMClient | None:
    ep = settings.diagram_review_endpoint or settings.diagram_codegen_endpoint
    key = settings.diagram_review_api_key or settings.diagram_codegen_api_key
    model = settings.diagram_review_model or settings.diagram_codegen_model
    if not ep or not key or not model:
        return None
    return LLMClient(ep, key, model)


def _get_image_size(image_bytes: bytes) -> tuple[int, int]:
    img = Image.open(io.BytesIO(image_bytes))
    return img.size


async def generate_image(
    description: str,
    mode: Literal["generate", "edit", "cut"] = "generate",
    *,
    source: Path | None = None,
    mask: Path | None = None,
    style: str = "",
    size: str = "1024x1024",
    background: str = "auto",
    output_format: str = "png",
    output_dir: Path,
    settings: Settings | None = None,
) -> ImageResult:
    """Generate, edit, or cut an image."""
    if settings is None:
        settings = Settings()

    output_dir.mkdir(parents=True, exist_ok=True)
    client = _build_image_client(settings)

    if mode == "generate":
        image_bytes = await _generate(
            client,
            description,
            style=style,
            size=size,
            background=background,
            output_format=output_format,
        )
    elif mode == "edit":
        if source is None:
            raise ValueError("source is required for edit mode")
        image_bytes = await _edit(
            client,
            description,
            source=source,
            mask=mask,
            size=size,
        )
    elif mode == "cut":
        if source is None:
            raise ValueError("source is required for cut mode")
        image_bytes = await _cut(
            client,
            description,
            source=source,
            settings=settings,
        )
    else:
        raise ValueError(f"Unknown mode: {mode}")

    ext = "webp" if output_format == "webp" else "png"
    out_name = f"{uuid.uuid4().hex[:8]}_generated.{ext}"
    out_path = output_dir / out_name
    out_path.write_bytes(image_bytes)

    effective_prompt = f"{style}: {description}" if style and mode == "generate" else description
    return ImageResult(
        image_path=out_path,
        mode=mode,
        prompt=effective_prompt,
        size=_get_image_size(image_bytes),
    )


async def _generate(
    client: ImageClient,
    description: str,
    *,
    style: str,
    size: str,
    background: str,
    output_format: str,
) -> bytes:
    prompt = f"{style}: {description}" if style else description
    return await client.generate(
        prompt,
        size=size,
        background=background,
        output_format=output_format,
    )


async def _edit(
    client: ImageClient,
    description: str,
    *,
    source: Path,
    mask: Path | None,
    size: str,
) -> bytes:
    image_bytes = source.read_bytes()
    mask_bytes = mask.read_bytes() if mask else None
    return await client.edit(image_bytes, description, mask=mask_bytes, size=size)


async def _cut(
    client: ImageClient,
    description: str,
    *,
    source: Path,
    settings: Settings,
) -> bytes:
    """Two-strategy cut: VLM-guided crop, then gpt-image-2 fallback."""
    source_bytes = source.read_bytes()

    # Strategy 1: VLM-guided crop
    vlm = _build_vlm_client(settings)
    if vlm:
        try:
            image_b64 = base64.b64encode(source_bytes).decode()
            prompt = _CUT_VLM_PROMPT.format(description=description)
            result = await vlm.chat_structured(
                [{"role": "user", "content": prompt}],
                _BBOX_SCHEMA,
                tool_name="find_elements",
                image_b64=image_b64,
            )
            elements = result.get("elements", [])
            if not elements:
                raise ValueError("VLM found no matching elements")
            bbox = elements[0]  # Use first match
            logger.info("VLM found %d element(s), using: %s", len(elements), bbox.get("label"))
            x, y = int(bbox["x"]), int(bbox["y"])
            w, h = int(bbox["width"]), int(bbox["height"])

            img = Image.open(io.BytesIO(source_bytes))
            cropped = img.crop((x, y, x + w, y + h))
            buf = io.BytesIO()
            cropped.save(buf, format="PNG")
            cropped_bytes = buf.getvalue()

            # Try background removal
            try:
                from ._image_segment import extract_element

                return extract_element(cropped_bytes, description)
            except (ImportError, ModuleNotFoundError):
                return cropped_bytes

        except Exception:
            logger.warning("VLM-guided crop failed, falling back to image edit", exc_info=True)

    # Strategy 2: gpt-image-2 isolate
    isolate_prompt = (
        f"Isolate only the following element from this image: {description}. "
        "Remove everything else. Use a transparent background."
    )
    return await client.edit(source_bytes, isolate_prompt)
