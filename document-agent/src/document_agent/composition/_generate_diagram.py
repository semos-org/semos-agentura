"""Public API for diagram generation with optimization."""

from __future__ import annotations

import logging
from functools import partial
from pathlib import Path
from typing import Literal

from .._llm_client import LLMClient
from .._utils import require_tool
from ..config import Settings
from ..exceptions import ProviderError
from ..models import DiagramResult
from ._diagram_optimize import optimize_diagram
from ._diagram_source import DiagramSource, extract_diagram_source, restore_embedded_images
from ._drawio import render_drawio_to_png
from ._mermaid import render_mermaid_to_png

logger = logging.getLogger(__name__)


def _build_client(
    endpoint: str | None,
    api_key: str | None,
    model: str | None,
    label: str,
) -> LLMClient:
    if not endpoint or not api_key or not model:
        raise ProviderError(f"{label} LLM not configured. Set {label.upper()}_ENDPOINT, _API_KEY, _MODEL.")
    return LLMClient(endpoint, api_key, model)


async def generate_diagram(
    description: str | None = None,
    diagram_type: Literal["mermaid", "drawio"] | None = None,
    *,
    source: Path | str | None = None,
    output_dir: Path,
    max_iterations: int = 3,
    settings: Settings | None = None,
) -> DiagramResult:
    """Generate a diagram from description and/or existing source.

    Args:
        description: Natural-language description or modification
            instructions. Optional when source is provided.
        diagram_type: "mermaid" or "drawio". Auto-detected from
            source if not specified. Defaults to "mermaid".
        source: Existing diagram - file path or inline code string.
        output_dir: Where to write intermediate and final images.
        max_iterations: Max generate-render-review cycles.
        settings: Settings (auto-loaded if None).
    """
    if not description and not source:
        raise ValueError(
            "At least one of description or source is required",
        )

    if settings is None:
        settings = Settings()

    # Build codegen client
    codegen = _build_client(
        settings.diagram_codegen_endpoint,
        settings.diagram_codegen_api_key,
        settings.diagram_codegen_model,
        "diagram_codegen",
    )

    # Build review client (fallback to codegen settings)
    review = _build_client(
        settings.diagram_review_endpoint or settings.diagram_codegen_endpoint,
        settings.diagram_review_api_key or settings.diagram_codegen_api_key,
        settings.diagram_review_model or settings.diagram_codegen_model,
        "diagram_review",
    )

    # Extract source diagram if provided
    diagram_source: DiagramSource | None = None
    if source is not None:
        diagram_source = await extract_diagram_source(
            source,
            codegen_client=codegen,
            settings=settings,
        )
        # Auto-detect type from source if not specified
        if diagram_type is None and diagram_source.diagram_type != "unknown":
            diagram_type = diagram_source.diagram_type

    # Default to mermaid
    if diagram_type is None:
        diagram_type = "mermaid"

    # Build render function
    if diagram_type == "mermaid":
        mmdc = require_tool("mmdc", settings.mmdc_path)
        render_fn = partial(
            render_mermaid_to_png,
            mmdc_path=mmdc,
        )
    else:
        drawio = require_tool("drawio", settings.drawio_path)
        _base_render = partial(
            render_drawio_to_png,
            drawio_path=drawio,
            drawio_desktop_path=settings.drawio_desktop_path,
        )
        # Wrap render to restore embedded images before rendering
        # (the LLM works on stripped XML, but rendering needs full images)
        embedded = diagram_source.embedded_images if diagram_source else None
        if embedded:

            def render_fn(code: str, path: Path) -> Path:
                full_code = restore_embedded_images(code, embedded)
                return _base_render(full_code, path)
        else:
            render_fn = _base_render

    result = await optimize_diagram(
        description,
        diagram_type,
        source=diagram_source,
        max_iterations=max_iterations,
        codegen_client=codegen,
        review_client=review,
        render_fn=render_fn,
        output_dir=output_dir,
    )

    # Restore embedded images in the final code output
    if diagram_source and diagram_source.embedded_images and result.code:
        result.code = restore_embedded_images(result.code, diagram_source.embedded_images)
        logger.info("Restored %d embedded images into output", len(diagram_source.embedded_images))

    return result
