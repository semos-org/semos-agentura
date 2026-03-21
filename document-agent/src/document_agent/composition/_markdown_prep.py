"""Markdown preprocessing: extract base64 images to disk for external tools."""

from __future__ import annotations

import base64
import logging
import re
import tempfile
from pathlib import Path

logger = logging.getLogger(__name__)

_DATA_URI_RE = re.compile(r"!\[([^\]]*)\]\((data:image/([^;]+);base64,([^)]+))\)")


def extract_base64_images(markdown: str, output_dir: Path) -> str:
    """Extract base64-inlined images from markdown, save to disk, replace with file paths.

    This is needed because tools like marp and pandoc need images as files.
    """
    output_dir.mkdir(parents=True, exist_ok=True)
    counter = 0

    def _replace(m: re.Match) -> str:
        nonlocal counter
        counter += 1
        alt = m.group(1)
        ext = m.group(3)
        if ext == "jpeg":
            ext = "jpg"
        b64_data = m.group(4)
        img_name = f"_extracted_{counter:03d}.{ext}"
        img_path = output_dir / img_name
        img_path.write_bytes(base64.b64decode(b64_data))
        return f"![{alt}]({img_name})"

    return _DATA_URI_RE.sub(_replace, markdown)


def prepare_markdown_file(
    source: Path | str,
    work_dir: Path,
) -> Path:
    """Ensure markdown is written to a file in work_dir with images extracted.

    Returns the path to the prepared markdown file.
    """
    if isinstance(source, Path) and source.exists():
        md_text = source.read_text(encoding="utf-8")
    else:
        md_text = str(source)

    # Extract any base64 images to the work dir
    md_text = extract_base64_images(md_text, work_dir)

    md_path = work_dir / "input.md"
    md_path.write_text(md_text, encoding="utf-8")
    return md_path
