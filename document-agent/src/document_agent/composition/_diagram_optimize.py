"""LLM-powered diagram generation with render-review optimization."""

from __future__ import annotations

import base64
import json
import logging
import re
from collections.abc import Callable
from pathlib import Path
from typing import Literal

from .._llm_client import LLMClient
from ..models import DiagramResult
from ._diagram_source import DiagramSource

logger = logging.getLogger(__name__)


def _downscale_image_b64(raw_b64: str, target_b64_len: int) -> str:
    """Downscale a PNG/JPEG image so its base64 fits within target_b64_len.

    Uses progressive JPEG quality reduction, falling back to resize.
    Returns the base64 string (without data URI prefix).
    """
    import io

    try:
        from PIL import Image
    except ImportError:
        # No Pillow - just truncate (LLM gets a broken image but won't crash)
        logger.warning("Pillow not installed, cannot downscale image")
        return raw_b64[:target_b64_len]

    raw_bytes = base64.b64decode(raw_b64)
    img = Image.open(io.BytesIO(raw_bytes))

    # Try JPEG quality reduction first
    for quality in (60, 40, 20, 10):
        buf = io.BytesIO()
        rgb = img.convert("RGB") if img.mode != "RGB" else img
        rgb.save(buf, format="JPEG", quality=quality, optimize=True)
        b64 = base64.b64encode(buf.getvalue()).decode()
        if len(b64) <= target_b64_len:
            logger.info("Downscaled image: quality=%d, %d -> %d chars", quality, len(raw_b64), len(b64))
            return b64

    # Still too large - resize progressively
    for scale in (0.5, 0.25, 0.1):
        w, h = int(img.width * scale), int(img.height * scale)
        if w < 50 or h < 50:
            break
        resized = img.resize((w, h), Image.LANCZOS)
        buf = io.BytesIO()
        resized.convert("RGB").save(buf, format="JPEG", quality=30, optimize=True)
        b64 = base64.b64encode(buf.getvalue()).decode()
        if len(b64) <= target_b64_len:
            logger.info("Downscaled image: scale=%.0f%%, %d -> %d chars", scale * 100, len(raw_b64), len(b64))
            return b64

    logger.warning("Could not downscale image to target size, using smallest version")
    return b64


_CODEGEN_SYSTEM_MERMAID = """\
You are an expert at creating Mermaid diagrams. \
Generate valid Mermaid diagram code for the user's description. \
Return ONLY the raw Mermaid code - no markdown fences, \
no explanation, no comments outside the diagram."""

_CODEGEN_SYSTEM_DRAWIO = """\
You are an expert at creating draw.io diagrams in XML format. \
Generate valid draw.io/mxGraph XML for the user's description. \
Return ONLY the raw XML - no markdown fences, no explanation.

CRITICAL layout rules to prevent arrow overlap and entanglement:
- Use explicit exitX/exitY and entryX/entryY on EVERY edge to \
control exactly where arrows connect to shapes.
- Leave generous spacing (at least 120px) between groups so \
arrows have clear routing channels.
- Never route arrows through or across boxes/groups.
- Use different sides of shapes for different connections \
(e.g. top for input, right for output, bottom for feedback).
- For orthogonal edges, plan the routing so paths do not cross \
each other. Stagger connection points if multiple arrows enter \
the same side of a shape.
- Place edge labels as separate mxCell text elements positioned \
near the midpoint of the arrow path, NOT as edge value attributes.
- Avoid placing any element (box, label, arrow) in the routing \
channel between two connected groups.

Visual style rules:
- Use swimlane containers (style="swimlane;startSize=28;...") \
for groups with bold 14px titles and colored fills.
- Use rounded=1 inner boxes with matching fill colors inside \
each swimlane. Font size 12px.
- Use a consistent, distinct color for each group \
(e.g. blue, orange, purple, grey, green, red).
- All arrows must have endArrow=classic and be clearly visible."""

_REVIEW_SYSTEM = """\
You are a diagram quality reviewer. You will receive a rendered \
diagram image and the original description it was created from.

Evaluate the diagram on three criteria:
(a) COMPLETENESS - does it reflect ALL aspects of the description?
(b) VISUAL QUALITY - is it visually appealing? Check for: \
misaligned elements, label collisions, overlapping shapes, \
truncated text, poor spacing, unreadable fonts, arrows pointing \
to wrong targets, inconsistent styling.
(c) ARROW ROUTING - this is critical. Check specifically for: \
arrows that cross over or through boxes/groups, arrows that \
overlap each other on the same path, arrows that obscure text \
labels, arrows that take unnecessarily long detours, connection \
points that cause visual clutter by bunching together. Every \
arrow must have a clear, unobstructed path.

Respond with ONLY a JSON object (no markdown fences):
{"pass": true/false, "issues": ["issue1", ...], "suggestions": "..."}

Be strict. Only pass if all three criteria are fully satisfied."""


def _extract_code(text: str, diagram_type: str) -> str:
    """Strip markdown fences if the LLM wrapped its output."""
    # Try to extract from ```mermaid ... ``` or ```xml ... ```
    patterns = [
        r"```(?:mermaid|xml|drawio)\s*\n(.*?)```",
        r"```\s*\n(.*?)```",
    ]
    for pat in patterns:
        m = re.search(pat, text, re.DOTALL)
        if m:
            return m.group(1).strip()
    return text.strip()


def _parse_review(text: str) -> dict:
    """Parse the reviewer's JSON response, tolerant of wrapping."""
    # Strip markdown fences if present
    m = re.search(r"```(?:json)?\s*\n(.*?)```", text, re.DOTALL)
    raw = m.group(1).strip() if m else text.strip()
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        logger.warning("Could not parse review JSON: %s", raw[:200])
        return {"pass": False, "issues": ["Unparseable review"], "suggestions": raw[:500]}


def _build_initial_messages(
    system: str,
    description: str | None,
    source: DiagramSource | None,
    diagram_type: str,
) -> list[dict]:
    """Build the initial codegen message list based on inputs."""
    messages: list[dict] = [{"role": "system", "content": system}]

    if source and source.code:
        # Existing code - seed as assistant message, then refine
        messages.append(
            {"role": "assistant", "content": source.code},
        )
        if description:
            text = f"Refine this diagram: {description}\n\nReturn ONLY the updated {diagram_type} code."
        else:
            text = (
                "Improve this diagram for visual clarity "
                "and completeness. Return ONLY the updated "
                f"{diagram_type} code."
            )

        # If embedded images were stripped, include them as visual reference
        uris = (source.embedded_images or {}).get("uris", {})
        if uris:
            text += (
                "\n\nNote: Some mxCell elements contain image placeholders "
                "(__IMG_N__). Do NOT remove these cells - they will be "
                "restored automatically. Just edit the non-image cells."
            )
            content: list[dict] = [{"type": "text", "text": text}]
            # Attach images for visual reference (cap total to ~1 MB base64)
            max_total_b64 = 1_000_000
            total = 0
            attached = 0
            n_images = len(uris)
            per_image_budget = max_total_b64 // max(n_images, 1)
            for _pid, data_uri in uris.items():
                if "," in data_uri:
                    raw_b64 = data_uri.split(",", 1)[1]
                else:
                    raw_b64 = data_uri
                if len(raw_b64) > per_image_budget:
                    raw_b64 = _downscale_image_b64(raw_b64, per_image_budget)
                if total + len(raw_b64) > max_total_b64:
                    logger.info(
                        "Image budget exhausted, skipping remaining %d images",
                        n_images - attached,
                    )
                    break
                content.append(
                    {
                        "type": "image",
                        "source": {
                            "type": "base64",
                            "media_type": "image/png",
                            "data": raw_b64,
                        },
                    }
                )
                total += len(raw_b64)
                attached += 1
            if attached:
                content[0]["text"] += f" {attached} reference image(s) attached below."
            messages.append({"role": "user", "content": content})
        else:
            messages.append({"role": "user", "content": text})
    elif source and source.description:
        # VLM analysis of an image (hand-drawing, screenshot)
        prompt = (
            "Create a {type} diagram based on this analysis "
            "of an existing diagram:\n\n{analysis}\n\n"
            "{instructions}\n\n"
            "Return ONLY the {type} code."
        ).format(
            type=diagram_type,
            analysis=source.description,
            instructions=description or "Recreate it faithfully",
        )
        messages.append({"role": "user", "content": prompt})
    elif description:
        # Fresh generation from description only
        messages.append({"role": "user", "content": description})
    else:
        raise ValueError(
            "At least one of description or source must be provided",
        )

    return messages


async def optimize_diagram(
    description: str | None,
    diagram_type: Literal["mermaid", "drawio"],
    *,
    source: DiagramSource | None = None,
    max_iterations: int,
    codegen_client: LLMClient,
    review_client: LLMClient,
    render_fn: Callable[[str, Path], Path],
    output_dir: Path,
) -> DiagramResult:
    """Generate and iteratively refine a diagram.

    Args:
        description: Natural-language description or modification
            instructions. Optional when source is provided.
        diagram_type: "mermaid" or "drawio".
        source: Existing diagram to use as starting point.
        max_iterations: Maximum generate-render-review cycles.
        codegen_client: LLM client for code generation.
        review_client: LLM client for visual review (should
            differ from codegen_client to avoid self-bias).
        render_fn: Callable(code, output_path) -> output_path.
        output_dir: Directory for intermediate images.
    """
    output_dir.mkdir(parents=True, exist_ok=True)

    system = _CODEGEN_SYSTEM_MERMAID if diagram_type == "mermaid" else _CODEGEN_SYSTEM_DRAWIO

    codegen_messages = _build_initial_messages(
        system,
        description,
        source,
        diagram_type,
    )
    # Use source description for review context if no user description
    if not description and source and source.description:
        description = source.description

    review_log: list[dict] = []
    code = ""
    image_path = output_dir / "diagram.png"

    for iteration in range(1, max_iterations + 1):
        logger.info(
            "Diagram optimization iteration %d/%d",
            iteration,
            max_iterations,
        )

        # --- Generate ---
        raw = await codegen_client.chat(codegen_messages)
        code = _extract_code(raw, diagram_type)
        codegen_messages.append(
            {"role": "assistant", "content": raw},
        )

        # --- Render ---
        suffix = ".drawio.png" if diagram_type == "drawio" else ".png"
        iter_path = output_dir / f"iter_{iteration:02d}{suffix}"
        try:
            render_fn(code, iter_path)
        except Exception as exc:
            logger.warning("Render failed at iteration %d: %s", iteration, exc)
            # Ask codegen to fix the error
            codegen_messages.append(
                {
                    "role": "user",
                    "content": (
                        f"The diagram failed to render with error:\n"
                        f"{exc}\n\nFix the code. Return ONLY the "
                        f"corrected {diagram_type} code."
                    ),
                }
            )
            review_log.append(
                {
                    "iteration": iteration,
                    "render_error": str(exc),
                }
            )
            continue

        image_path = iter_path

        # Last iteration - skip review
        if iteration == max_iterations:
            logger.info("Max iterations reached, using current result")
            review_log.append(
                {
                    "iteration": iteration,
                    "skipped": "max iterations",
                }
            )
            break

        # --- Review (stateless - fresh messages each time) ---
        png_b64 = base64.b64encode(
            iter_path.read_bytes(),
        ).decode()

        review_messages = [
            {"role": "system", "content": _REVIEW_SYSTEM},
            {
                "role": "user",
                "content": (
                    f"Original description:\n{description or '(improve existing diagram)'}\n\n"
                    f"Review the rendered diagram image."
                ),
            },
        ]

        review_raw = await review_client.chat_with_image(
            review_messages,
            png_b64,
        )
        review = _parse_review(review_raw)
        review["iteration"] = iteration
        review_log.append(review)

        if review.get("pass"):
            logger.info("Diagram passed review at iteration %d", iteration)
            break

        # Feed issues back to codegen
        issues = review.get("issues", [])
        suggestions = review.get("suggestions", "")
        feedback = (
            "The diagram has these issues:\n"
            + "\n".join(f"- {i}" for i in issues)
            + f"\n\nSuggestions: {suggestions}\n\n"
            f"Fix all issues. Return ONLY the updated "
            f"{diagram_type} code."
        )
        codegen_messages.append({"role": "user", "content": feedback})
        logger.info(
            "Iteration %d: %d issues found, refining...",
            iteration,
            len(issues),
        )

    return DiagramResult(
        code=code,
        image_path=image_path,
        iterations=len(review_log),
        review_log=review_log,
    )
