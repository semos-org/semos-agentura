"""Image extraction, base64 inlining, and markdown assembly."""

from __future__ import annotations

import base64
import json
import logging
import re
from pathlib import Path
from typing import Any
from urllib.parse import quote as urlquote

from ._ocr_models import OCRResponse

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Annotation parsing
# ---------------------------------------------------------------------------


def _extract_from_malformed_json(raw: str) -> dict | None:
    """Try to extract key-value pairs from truncated/malformed JSON."""
    result = {}
    for key in ("image_type", "text_content", "description"):
        m = re.search(rf'"{key}"\s*:\s*"((?:[^"\\]|\\.)*)"', raw)
        if m:
            result[key] = m.group(1)
        else:
            m = re.search(rf'"{key}"\s*:\s*"(.*)', raw, re.DOTALL)
            if m:
                result[key] = m.group(1).rstrip()
    return result if result else None


def _parse_annotation(raw: str | Any) -> dict:
    """Parse a JSON image annotation into a dict."""
    try:
        data = json.loads(raw) if isinstance(raw, str) else raw
    except (json.JSONDecodeError, TypeError):
        if isinstance(raw, str):
            extracted = _extract_from_malformed_json(raw)
            if extracted:
                return extracted
        return {"description": str(raw)}
    return data if isinstance(data, dict) else {"description": str(data)}


# ---------------------------------------------------------------------------
# Image saving to disk
# ---------------------------------------------------------------------------


def _encode_md_path(rel_path: str) -> str:
    """URL-encode a relative path for markdown syntax."""
    return "/".join(urlquote(seg, safe="") for seg in rel_path.split("/"))


def save_images(
    ocr_response: OCRResponse, output_dir: Path, stem: str
) -> tuple[dict[str, str], dict[str, dict]]:
    """Extract base64 images to disk and collect annotations.

    Returns:
        image_map: image id -> relative file path
        annotation_map: image id -> parsed annotation dict
    """
    image_map: dict[str, str] = {}
    annotation_map: dict[str, dict] = {}
    img_dir = output_dir / f"{stem}_images"
    counter = 0

    for page in ocr_response.pages:
        for img in page.images:
            if img.image_annotation:
                annotation_map[img.id] = _parse_annotation(img.image_annotation)
            if not img.image_base64:
                continue
            img_dir.mkdir(parents=True, exist_ok=True)
            counter += 1
            ext = "png"
            if img.image_base64.startswith("data:"):
                match = re.match(r"data:image/(\w+);base64,", img.image_base64)
                if match:
                    ext = match.group(1)
                    if ext == "jpeg":
                        ext = "jpg"
            img_filename = f"{stem}_{counter:03d}.{ext}"
            img_path = img_dir / img_filename

            b64_data = img.image_base64
            if "," in b64_data:
                b64_data = b64_data.split(",", 1)[1]

            img_path.write_bytes(base64.b64decode(b64_data))
            image_map[img.id] = f"{stem}_images/{img_filename}"

    return image_map, annotation_map


# ---------------------------------------------------------------------------
# Inline images as base64 (for INLINE output mode)
# ---------------------------------------------------------------------------


def collect_annotations(ocr_response: OCRResponse) -> dict[str, dict]:
    """Collect all image annotations from the OCR response."""
    annotation_map: dict[str, dict] = {}
    for page in ocr_response.pages:
        for img in page.images:
            if img.image_annotation:
                annotation_map[img.id] = _parse_annotation(img.image_annotation)
    return annotation_map


def inline_images_as_base64(
    ocr_response: OCRResponse,
    annotation_map: dict[str, dict],
) -> str:
    """Combine per-page markdown with all images inlined as base64 data URIs."""
    # Build a map of image id -> base64 data URI
    b64_map: dict[str, str] = {}
    for page in ocr_response.pages:
        for img in page.images:
            if not img.image_base64:
                continue
            data_uri = img.image_base64
            if not data_uri.startswith("data:"):
                data_uri = f"data:image/png;base64,{data_uri}"
            b64_map[img.id] = data_uri

    pages = []
    for page in ocr_response.pages:
        md = page.resolve_tables()
        # Replace image references with base64 data URIs
        for img_id, data_uri in b64_map.items():
            ann = annotation_map.get(img_id, {})
            alt_text = _build_alt_text(ann)
            old = f"![{img_id}]({img_id})"
            new = f"![{alt_text}]({data_uri})" if alt_text else f"![]({data_uri})"
            md = md.replace(old, new)
            # Also handle other reference patterns
            md = re.sub(
                rf"!\[([^\]]*)\]\({re.escape(img_id)}\)",
                lambda m, at=alt_text, du=data_uri: f"![{at or m.group(1)}]({du})",
                md,
            )
        md = _clean_json_alt_text(md)
        pages.append(md)
    return "\n\n".join(pages)


# ---------------------------------------------------------------------------
# Markdown assembly (file mode)
# ---------------------------------------------------------------------------


def _build_alt_text(ann: dict) -> str:
    """Build alt text string from an annotation dict."""
    parts = []
    if ann.get("image_type"):
        parts.append(ann["image_type"])
    if ann.get("description"):
        parts.append(ann["description"])
    if ann.get("text_content"):
        parts.append(f"text: {ann['text_content']}")
    return " | ".join(parts)


def replace_images_in_markdown(
    markdown: str,
    image_map: dict[str, str],
    annotation_map: dict[str, dict],
) -> str:
    """Replace image references with file paths and annotation alt text."""
    for img_id, rel_path in image_map.items():
        encoded_path = _encode_md_path(rel_path)
        alt_text = _build_alt_text(annotation_map.get(img_id, {}))

        old = f"![{img_id}]({img_id})"
        new = f"![{alt_text}]({encoded_path})" if alt_text else f"![]({encoded_path})"
        markdown = markdown.replace(old, new)

        markdown = re.sub(
            rf"!\[([^\]]*)\]\({re.escape(img_id)}\)",
            lambda m, at=alt_text, ep=encoded_path: f"![{at or m.group(1)}]({ep})",
            markdown,
        )

    # Strip remaining data URI images that weren't mapped
    markdown = re.sub(
        r"!\[([^\]]*)\]\(data:image/[^)]+\)",
        lambda m: f"![{m.group(1)}]",
        markdown,
    )

    # Append annotations for images without saved files
    for img_id, ann in annotation_map.items():
        if img_id not in image_map:
            parts = []
            if ann.get("description"):
                parts.append(f"*{ann['description']}*")
            if ann.get("text_content"):
                parts.append(ann["text_content"])
            if parts:
                markdown += "\n\n" + "\n\n".join(parts)

    markdown = _clean_json_alt_text(markdown)
    return markdown


def _clean_json_alt_text(markdown: str) -> str:
    """Replace raw JSON objects in image alt text with formatted text."""

    def _replace_json_alt(m: re.Match) -> str:
        raw_json = m.group(1).strip()
        path = m.group(2)
        try:
            data = json.loads(raw_json)
        except (json.JSONDecodeError, TypeError):
            data = _extract_from_malformed_json(raw_json)
            if not data:
                return m.group(0)
        alt_text = _build_alt_text(data)
        return f"![{alt_text}]({path})"

    markdown = re.sub(r"!\[\s*(\{[\s\S]*?\})\s*\]\(([^)]+)\)", _replace_json_alt, markdown)
    markdown = re.sub(r"!\[(\s*\{[^}]*?)\]\(([^)]+)\)", _replace_json_alt, markdown)
    return markdown


def combine_markdown(
    ocr_response: OCRResponse,
    image_map: dict[str, str],
    annotation_map: dict[str, dict],
) -> str:
    """Combine per-page markdown into a single document."""
    pages = []
    for page in ocr_response.pages:
        md = replace_images_in_markdown(page.resolve_tables(), image_map, annotation_map)
        pages.append(md)
    return "\n\n".join(pages)
