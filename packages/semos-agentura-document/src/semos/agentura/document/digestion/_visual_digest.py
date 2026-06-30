"""Visual digest: VLM inspection of rendered document/form pages.

Renders a document's pages to images and runs a VLM pass that extracts
content - especially form field labels and their filled values - and flags
placement/truncation issues. Read-only; no form filling.
"""

from __future__ import annotations

import asyncio
import base64
import logging
from pathlib import Path

from .._render import downscale_image_b64, render_document_pages
from ..config import Settings
from ..models import DigestResult, OutputMode

logger = logging.getLogger(__name__)

_PAGES_PER_CALL = 2
_IMAGE_B64_BUDGET = 1_300_000

_VISUAL_SYSTEM = """\
You read a rendered document or form page and transcribe it as Markdown.
Focus on FORM CONTENT: for every field, output a line "Label: value" using
the printed caption as the label and the filled-in text as the value. Keep
section headings as Markdown headings. If a value looks cut off by its field
box, append " (truncated)". Leave empty fields out unless their label is a
meaningful heading. Output only the Markdown transcription for this page."""


def digest_visual(
    file_path: Path,
    *,
    output_dir: Path,
    output_mode: OutputMode = OutputMode.INLINE,
    settings: Settings | None = None,
) -> DigestResult:
    """Render pages and extract their content via a VLM."""
    if settings is None:
        settings = Settings()

    work_dir = output_dir / f"{file_path.stem}_visual"
    pages = render_document_pages(file_path, work_dir, max_pages=settings.max_pdf_pages)
    if not pages:
        raise ValueError(f"No pages rendered for {file_path.name}")

    client = _build_review_client(settings)
    markdown = asyncio.run(_extract_all(client, pages))

    if output_mode == OutputMode.FILE:
        md_path = output_dir / f"{file_path.stem}.md"
        md_path.parent.mkdir(parents=True, exist_ok=True)
        md_path.write_text(markdown, encoding="utf-8")
        logger.info("Written: %s", md_path)
        return DigestResult(markdown=markdown, output_path=md_path)

    return DigestResult(markdown=markdown)


def _build_review_client(settings: Settings):
    from ..composition._generate_diagram import _build_client

    return _build_client(
        settings.diagram_review_endpoint or settings.diagram_codegen_endpoint,
        settings.diagram_review_api_key or settings.diagram_codegen_api_key,
        settings.diagram_review_model or settings.diagram_codegen_model,
        "diagram_review",
    )


async def _extract_all(client, pages: list[Path]) -> str:
    """Transcribe each page batch and concatenate the Markdown."""
    sections: list[str] = []
    for start in range(0, len(pages), _PAGES_PER_CALL):
        batch = pages[start : start + _PAGES_PER_CALL]
        imgs = [downscale_image_b64(base64.b64encode(p.read_bytes()).decode(), _IMAGE_B64_BUDGET) for p in batch]
        messages = [
            {"role": "system", "content": _VISUAL_SYSTEM},
            {"role": "user", "content": "Transcribe the form content from the attached page(s)."},
        ]
        try:
            text = await client.chat_with_images(messages, imgs, max_tokens=8000)
        except Exception:
            logger.warning("Visual digest call failed for pages %d+", start + 1, exc_info=True)
            text = ""
        if text:
            sections.append(text.strip())
    return "\n\n".join(sections)
