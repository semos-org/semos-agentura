"""Extract and render Mermaid diagrams from Markdown."""

from __future__ import annotations

import base64
import logging
import os
import re
import subprocess
import tempfile
from pathlib import Path

from ..exceptions import MermaidRenderError

logger = logging.getLogger(__name__)

_MERMAID_BLOCK_RE = re.compile(r"```mermaid\s*\n(.*?)```", re.DOTALL)


def find_mermaid_blocks(markdown: str) -> list[tuple[int, int, str]]:
    """Find ```mermaid fenced code blocks.

    Returns list of (start, end, diagram_code) tuples.
    """
    results = []
    for m in _MERMAID_BLOCK_RE.finditer(markdown):
        results.append((m.start(), m.end(), m.group(1).strip()))
    return results


def render_mermaid_to_png(code: str, output_path: Path, *, mmdc_path: Path) -> Path:
    """Render a mermaid diagram to PNG using mmdc CLI."""
    with tempfile.NamedTemporaryFile(suffix=".mmd", mode="w", delete=False, encoding="utf-8") as f:
        f.write(code)
        input_path = Path(f.name)

    try:
        cmd = [str(mmdc_path), "-i", str(input_path), "-o", str(output_path), "-b", "transparent", "-s", "4"]
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=60, shell=(os.name == "nt"))
        if result.returncode != 0:
            raise MermaidRenderError(f"mmdc failed: {result.stderr}")
        if not output_path.exists():
            raise MermaidRenderError(f"mmdc did not produce output: {output_path}")
        return output_path
    finally:
        input_path.unlink(missing_ok=True)


def render_mermaid_to_base64(code: str, *, mmdc_path: Path) -> str:
    """Render a mermaid diagram to PNG and return as base64 data URI."""
    with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as f:
        output_path = Path(f.name)

    try:
        render_mermaid_to_png(code, output_path, mmdc_path=mmdc_path)
        png_bytes = output_path.read_bytes()
        b64 = base64.b64encode(png_bytes).decode()
        return f"data:image/png;base64,{b64}"
    finally:
        output_path.unlink(missing_ok=True)


def replace_mermaid_blocks(
    markdown: str,
    *,
    output_dir: Path | None = None,
    inline: bool = False,
    mmdc_path: Path,
) -> str:
    """Replace all ```mermaid blocks with rendered PNG images.

    If inline=True, images are base64-inlined. Otherwise saved to output_dir.
    """
    blocks = find_mermaid_blocks(markdown)
    if not blocks:
        return markdown

    # Process in reverse order to preserve offsets
    for i, (start, end, code) in enumerate(reversed(blocks)):
        idx = len(blocks) - 1 - i
        logger.info("Rendering mermaid diagram %d/%d...", idx + 1, len(blocks))
        try:
            if inline:
                data_uri = render_mermaid_to_base64(code, mmdc_path=mmdc_path)
                replacement = f"![mermaid diagram]({data_uri})"
            else:
                if output_dir is None:
                    raise MermaidRenderError("output_dir required for file-based mermaid rendering")
                output_dir.mkdir(parents=True, exist_ok=True)
                img_name = f"mermaid_{idx + 1:03d}.png"
                img_path = output_dir / img_name
                render_mermaid_to_png(code, img_path, mmdc_path=mmdc_path)
                replacement = f"![]({img_name})"
            markdown = markdown[:start] + replacement + markdown[end:]
        except MermaidRenderError:
            logger.warning("Failed to render mermaid diagram %d, keeping as code block", idx + 1)

    return markdown
