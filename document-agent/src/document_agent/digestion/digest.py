"""Main entry point for document digestion (OCR to Markdown)."""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from mistralai.extra.utils.response_format import response_format_from_pydantic_model
from pydantic import BaseModel

from .._constants import OFFICE_EXTENSIONS, PDF_EXTENSIONS, SUPPORTED_EXTENSIONS
from .._utils import resolve_source
from ..config import Settings
from ..exceptions import DocumentAgentError
from ..models import DigestResult, ImageDescription, OutputMode
from ._images import collect_annotations, combine_markdown, inline_images_as_base64, save_images
from ._ocr_models import OCRResponse
from ._office import convert_office_to_pdf
from ._pdf import merge_ocr_responses, split_pdf
from ._providers import get_provider, register_schema
from ._schema import load_schema

logger = logging.getLogger(__name__)


def digest(
    source: Path | str | bytes,
    *,
    filename: str | None = None,
    output_dir: Path | None = None,
    output_mode: OutputMode = OutputMode.FILE,
    schema: type[BaseModel] | str | None = None,
    annotation_prompt: str | None = None,
    max_pages: int | None = None,
    settings: Settings | None = None,
) -> DigestResult:
    """Digest a document into Markdown.

    Args:
        source: File path, base64-encoded string, or raw bytes.
        filename: Required when source is bytes/base64 (to determine file type).
        output_dir: Where to write output files (default: source directory or cwd).
        output_mode: FILE writes to disk; INLINE returns markdown with base64 images.
        schema: Pydantic BaseModel class or path to .py file for structured extraction.
        annotation_prompt: Prompt for structured extraction (requires schema).
        max_pages: Override max pages per PDF chunk.
        settings: Settings instance (auto-created from env if None).

    Returns:
        DigestResult with markdown content and optional paths/annotations.
    """
    if settings is None:
        settings = Settings()
    if max_pages is None:
        max_pages = settings.max_pdf_pages

    # Resolve source to a file path
    file_path, is_temp = resolve_source(source, filename)
    try:
        return _digest_file(
            file_path,
            output_dir=output_dir,
            output_mode=output_mode,
            schema=schema,
            annotation_prompt=annotation_prompt,
            max_pages=max_pages,
            settings=settings,
        )
    finally:
        if is_temp:
            file_path.unlink(missing_ok=True)


def _digest_file(
    file_path: Path,
    *,
    output_dir: Path | None,
    output_mode: OutputMode,
    schema: type[BaseModel] | str | None,
    annotation_prompt: str | None,
    max_pages: int,
    settings: Settings,
) -> DigestResult:
    ext = file_path.suffix.lower()
    if ext not in SUPPORTED_EXTENSIONS:
        raise DocumentAgentError(f"Unsupported file type: {ext}")

    # Convert Office documents to PDF first
    temp_pdf: Path | None = None
    if ext in OFFICE_EXTENSIONS:
        temp_pdf = convert_office_to_pdf(file_path, settings.libre_office_path)
        file_path = temp_pdf
        ext = ".pdf"

    try:
        return _ocr_and_assemble(
            file_path,
            original_stem=file_path.stem if temp_pdf is None else Path(file_path.name).stem.split("_part")[0],
            output_dir=output_dir or file_path.parent,
            output_mode=output_mode,
            schema=schema,
            annotation_prompt=annotation_prompt,
            max_pages=max_pages,
            settings=settings,
        )
    finally:
        if temp_pdf:
            temp_pdf.unlink(missing_ok=True)


def _ocr_and_assemble(
    file_path: Path,
    *,
    original_stem: str,
    output_dir: Path,
    output_mode: OutputMode,
    schema: type[BaseModel] | str | None,
    annotation_prompt: str | None,
    max_pages: int,
    settings: Settings,
) -> DigestResult:
    provider = get_provider(settings)

    # Resolve schema
    schema_cls: type[BaseModel] | None = None
    if isinstance(schema, str):
        schema_cls = load_schema(schema)
    elif schema is not None:
        schema_cls = schema

    # Build OCR kwargs
    ocr_kwargs: dict[str, Any] = {
        "bbox_annotation_format": response_format_from_pydantic_model(ImageDescription),
        "table_format": settings.table_format,
    }
    if schema_cls is not None:
        register_schema(schema_cls)
        ocr_kwargs["document_annotation_format"] = response_format_from_pydantic_model(schema_cls)
        if annotation_prompt:
            ocr_kwargs["document_annotation_prompt"] = annotation_prompt

    # Auto-split large PDFs
    ext = file_path.suffix.lower()
    chunks: list[Path] = []
    if ext in PDF_EXTENSIONS:
        chunks = split_pdf(file_path, max_pages=max_pages)

    if chunks:
        logger.info("PDF split into %d chunks of up to %d pages", len(chunks), max_pages)
        try:
            responses: list[OCRResponse] = []
            for i, chunk_path in enumerate(chunks, 1):
                logger.info("Processing chunk %d/%d...", i, len(chunks))
                responses.append(provider.ocr(chunk_path, **ocr_kwargs))
            response = merge_ocr_responses(responses)
        finally:
            for chunk_path in chunks:
                chunk_path.unlink(missing_ok=True)
    else:
        logger.info("Processing: %s", file_path.name)
        response = provider.ocr(file_path, **ocr_kwargs)

    stem = original_stem

    # Assemble markdown based on output mode
    if output_mode == OutputMode.INLINE:
        annotation_map = collect_annotations(response)
        markdown = inline_images_as_base64(response, annotation_map)
        result = DigestResult(markdown=markdown)
    else:
        image_map, annotation_map = save_images(response, output_dir, stem)
        markdown = combine_markdown(response, image_map, annotation_map)
        md_path = output_dir / f"{stem}.md"
        md_path.write_text(markdown, encoding="utf-8")
        logger.info("Written: %s", md_path)
        result = DigestResult(
            markdown=markdown,
            output_path=md_path,
            images_dir=output_dir / f"{stem}_images" if image_map else None,
        )

    # Handle structured annotation
    if schema_cls is not None and response.document_annotation:
        ann_data = response.document_annotation
        if isinstance(ann_data, str):
            ann_data = json.loads(ann_data)
        result.annotation = ann_data
        if output_mode == OutputMode.FILE:
            json_path = output_dir / f"{stem}.json"
            json_path.write_text(json.dumps(ann_data, indent=2, ensure_ascii=False), encoding="utf-8")
            result.annotation_path = json_path
            logger.info("Written: %s", json_path)

    return result
