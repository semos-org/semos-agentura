"""Pandoc-based DOCX digestion - preserves footnotes, tracked changes, comments."""

from __future__ import annotations

import base64
import logging
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

from mistralai.extra.utils.response_format import response_format_from_pydantic_model

from .._utils import find_tool
from ..config import Settings
from ..exceptions import DocumentAgentError
from ..models import DigestResult, ImageDescription, OutputMode
from ._images import _build_alt_text, _encode_md_path
from ._providers import get_provider
from ._styles import extract_styles, format_yaml_frontmatter

logger = logging.getLogger(__name__)

_IMG_REF_RE = re.compile(r"!\[([^\]]*)\]\(([^)]+)\)")


def digest_docx_with_pandoc(
    file_path: Path,
    *,
    output_dir: Path,
    output_mode: OutputMode = OutputMode.FILE,
    track_changes: str = "accept",
    describe_images: bool = False,
    include_styles: bool = True,
    pandoc_path: Path | None = None,
    settings: Settings | None = None,
) -> DigestResult:
    """Digest a DOCX/ODT file to Markdown using pandoc.

    Preserves footnotes, tracked changes, and comments that the OCR
    pipeline would lose.

    Args:
        file_path: Path to the DOCX or ODT file.
        output_dir: Directory for output files.
        output_mode: FILE writes to disk; INLINE returns base64 images.
        track_changes: "accept", "reject", or "all".
        describe_images: If True, send extracted images to VLM for annotation.
        include_styles: If True, prepend YAML front matter with extracted
            document styles (font, size, color, margins). These can be used
            by compose_document to reproduce the formatting.
        pandoc_path: Path to pandoc binary (auto-detected if None).
        settings: Settings instance (for VLM provider when describe_images=True).
    """
    if settings is None:
        settings = Settings()
    if pandoc_path is None:
        p = find_tool("pandoc", settings.pandoc_path)
        if p is None:
            raise DocumentAgentError("pandoc not found on PATH")
        pandoc_path = p

    stem = file_path.stem
    work_dir = Path(tempfile.mkdtemp(prefix="docx_digest_"))

    try:
        # Run pandoc
        md_text = _run_pandoc(file_path, work_dir, pandoc_path, track_changes)

        # Extract and prepend document styles as YAML front matter
        if include_styles:
            doc_styles = extract_styles(file_path)
            if doc_styles:
                md_text = format_yaml_frontmatter(doc_styles) + md_text

        # Normalize images (pandoc extracts to media/)
        media_dir = work_dir / "media"
        md_text, image_map = _normalize_pandoc_images(md_text, media_dir, stem, output_dir)

        # Optional VLM annotation
        if describe_images and image_map:
            md_text = _annotate_images(image_map, md_text, settings)

        # Build result based on output mode
        if output_mode == OutputMode.INLINE:
            md_text = _inline_images_as_base64(md_text, image_map)
            return DigestResult(markdown=md_text)

        # FILE mode - write markdown and report images dir
        md_path = output_dir / f"{stem}.md"
        md_path.parent.mkdir(parents=True, exist_ok=True)
        md_path.write_text(md_text, encoding="utf-8")
        images_dir = output_dir / f"{stem}_images"
        logger.info("Written: %s", md_path)
        return DigestResult(
            markdown=md_text,
            output_path=md_path,
            images_dir=images_dir if images_dir.exists() else None,
        )
    finally:
        shutil.rmtree(work_dir, ignore_errors=True)


def _run_pandoc(
    file_path: Path,
    work_dir: Path,
    pandoc_path: Path,
    track_changes: str,
) -> str:
    """Run pandoc to convert DOCX to Markdown."""
    if track_changes not in ("accept", "reject", "all"):
        raise DocumentAgentError(
            f"Invalid track_changes value: {track_changes!r}. Must be 'accept', 'reject', or 'all'."
        )

    cmd = [
        str(pandoc_path),
        str(file_path),
        "--from",
        "docx",
        "--to",
        "markdown",
        f"--track-changes={track_changes}",
        f"--extract-media={work_dir}",
        "--wrap=none",
    ]
    logger.info("Running: %s", " ".join(cmd))
    result = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=120,
        cwd=file_path.parent,
    )
    if result.returncode != 0:
        raise DocumentAgentError(f"Pandoc failed: {result.stderr}")
    return result.stdout


def _normalize_pandoc_images(
    markdown: str,
    media_dir: Path,
    stem: str,
    output_dir: Path,
) -> tuple[str, dict[str, Path]]:
    """Move pandoc-extracted images to OCR convention and update refs.

    Pandoc extracts to media/imageN.ext. This moves them to
    {stem}_images/{stem}_001.ext and updates the markdown references.

    Returns (updated_markdown, {relative_ref: absolute_path} map).
    """
    image_map: dict[str, Path] = {}
    if not media_dir.exists():
        return markdown, image_map

    images_dir = output_dir / f"{stem}_images"
    images_dir.mkdir(parents=True, exist_ok=True)

    # Collect and sort image files
    image_files = sorted(
        f
        for f in media_dir.iterdir()
        if f.is_file()
        and f.suffix.lower()
        in {
            ".png",
            ".jpg",
            ".jpeg",
            ".gif",
            ".svg",
            ".webp",
            ".bmp",
            ".tiff",
        }
    )
    if not image_files:
        images_dir.rmdir()
        return markdown, image_map

    for i, src in enumerate(image_files, 1):
        ext = src.suffix.lower()
        new_name = f"{stem}_{i:03d}{ext}"
        dest = images_dir / new_name
        shutil.copy2(src, dest)

        # Build the old reference (as pandoc writes it)
        old_ref = f"media/{src.name}"
        new_ref = f"{stem}_images/{new_name}"
        encoded_ref = _encode_md_path(new_ref)
        markdown = markdown.replace(old_ref, encoded_ref)

        image_map[encoded_ref] = dest
        logger.debug("Image: %s -> %s", old_ref, new_ref)

    return markdown, image_map


def _annotate_images(
    image_map: dict[str, Path],
    markdown: str,
    settings: Settings,
) -> str:
    """Send each extracted image to VLM for annotation, update alt text."""
    try:
        provider = get_provider(settings)
    except Exception:
        logger.warning("VLM provider not available, skipping image annotation")
        return markdown

    ann_format = response_format_from_pydantic_model(ImageDescription)

    for ref, img_path in image_map.items():
        try:
            response = provider.ocr(img_path, bbox_annotation_format=ann_format)
            # Get annotation from first page's first image, or page-level
            annotation = None
            if response.pages:
                page = response.pages[0]
                for img in page.images:
                    if img.image_annotation:
                        annotation = img.image_annotation
                        break
                if annotation is None and page.markdown:
                    # Use page markdown as fallback description
                    annotation = {"description": page.markdown.strip()[:200]}

            if annotation:
                import json

                if isinstance(annotation, str):
                    try:
                        annotation = json.loads(annotation)
                    except (json.JSONDecodeError, TypeError):
                        annotation = {"description": annotation}
                alt_text = _build_alt_text(annotation)
                if alt_text:
                    # Replace ![...](ref) with ![alt_text](ref)
                    escaped_ref = re.escape(ref)
                    markdown = re.sub(
                        rf"!\[[^\]]*\]\({escaped_ref}\)",
                        f"![{alt_text}]({ref})",
                        markdown,
                    )
        except Exception:
            logger.warning("Failed to annotate image %s", img_path.name, exc_info=True)

    return markdown


def _inline_images_as_base64(
    markdown: str,
    image_map: dict[str, Path],
) -> str:
    """Replace image file references with base64 data URIs."""
    from .._constants import MIME_MAP

    for ref, img_path in image_map.items():
        if not img_path.exists():
            continue
        ext = img_path.suffix.lstrip(".").lower()
        mime = MIME_MAP.get(ext, f"image/{ext}")
        b64 = base64.b64encode(img_path.read_bytes()).decode()
        data_uri = f"data:{mime};base64,{b64}"
        markdown = markdown.replace(ref, data_uri)

    return markdown
