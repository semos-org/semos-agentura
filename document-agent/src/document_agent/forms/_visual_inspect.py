"""Visual form inspection: learn cryptic field meanings via render + VLM.

When a form's keys are cryptic (e.g. ``sdt_0``, ``1015``) and not resolved by
a ``/TU`` tooltip, the meaning is recovered visually and merged into the
inspect schema. Two strategies:

- Set-of-Marks (PDF with field geometry): draw a numbered box at each field's
  rect on the rendered page and ask the VLM for the label per number. Works
  for every field type - including checkboxes - because it points at the
  exact location instead of relying on a locatable value.
- Probe values (no geometry, e.g. DOCX): fill unique tokens, render, and ask
  the VLM to locate each token and report its nearby label.

Mirrors the generate/render/review loop in composition/_diagram_optimize.py.
"""

from __future__ import annotations

import base64
import logging
import re
import tempfile
from pathlib import Path
from typing import Any

from .._render import downscale_image_b64, render_document_pages, render_pdf_pages_meta
from ..config import Settings
from ._schema import fields_to_data, fields_to_schema
from .fill import fill_form, inspect_form_fields

logger = logging.getLogger(__name__)

# Max field probes per VLM call - one page image is sent per call, and
# recall degrades sharply when too many fields are asked about at once.
_FIELDS_PER_CALL = 25
# Full-page windowed marking: badges are drawn on a sliding window of fields
# while the WHOLE page stays visible (so column headers / table structure are
# always in frame). Overlapping windows give each field multiple votes.
_WINDOW_SIZE = 12
_WINDOW_STEP = 6  # ~50% overlap
# Consecutive no-progress rounds tolerated before stopping (the VLM is
# non-deterministic, so stubborn fields often resolve on a retry).
_MAX_DRY_STREAK = 3
# Per-image base64 budget (~1.3 MB) to bound request size.
_IMAGE_B64_BUDGET = 1_300_000
# Full-page images need a larger budget to stay legible.
_PAGE_IMAGE_BUDGET = 2_400_000

_HUMAN_WORD_RE = re.compile(r"[A-Za-zÀ-ÿ]{4,}")

_REVIEW_SYSTEM = """\
You inspect a FILLED form to learn what each field means. Every field was \
filled with a unique probe value (e.g. PRB001). You are given the rendered \
page images and a list of {field_id: probe_value} pairs.

For each field_id, find where its probe value appears on the page and report:
- label: the human-readable caption printed next to or above that value
- description: a short note on what the field captures
- group: the section/heading the field belongs to (if any)
- page: 1-based page number where the value appears
- truncated: true if the value looks cut off by the field box
- found: true only if you actually located the probe value

Rely ONLY on what is visible. Do NOT infer labels from the field_id text. \
If a probe value cannot be found, set found=false and leave label empty."""

_REVIEW_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "fields": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "field_id": {"type": "string"},
                    "label": {"type": "string"},
                    "description": {"type": "string"},
                    "group": {"type": "string"},
                    "page": {"type": "integer"},
                    "truncated": {"type": "boolean"},
                    "found": {"type": "boolean"},
                },
                "required": ["field_id", "label", "found"],
            },
        }
    },
    "required": ["fields"],
}


async def inspect_form_visual(
    file_path: Path | str,
    *,
    max_iterations: int = 3,
    output_dir: Path | None = None,
    settings: Settings | None = None,
) -> dict:
    """Inspect a form and enrich cryptic field labels via visual review.

    Returns the ``{"schema", "data", "template"}`` contract. When
    ``output_dir`` is given, also fills the form with each field's discovered
    label (checkboxes ticked) and returns the path under ``validation_path``
    so the label-to-field mapping can be visually checked.
    """
    file_path = Path(file_path)
    if settings is None:
        settings = Settings()

    fields = inspect_form_fields(file_path)
    schema = fields_to_schema(fields)
    data = fields_to_data(fields)

    review_client = _build_review_client(settings)

    # Prefer geometry-based resolution when the PDF gives per-field rects: it
    # handles every field type (checkboxes included) and the LLM judges each
    # field's meaning. Fall back to probe values (DOCX without geometry).
    use_som = file_path.suffix.lower() == ".pdf" and any(
        f.get("rect") and f.get("page") is not None and not f.get("hidden") for f in fields
    )
    if use_som:
        findings = await _som_resolve(file_path, fields, review_client, settings, max_iterations=max_iterations)
    else:
        findings = await _probe_and_review(file_path, fields, review_client, settings, max_iterations=max_iterations)
    _merge_findings(schema, findings)

    result = {
        "schema": schema,
        "data": data,
        "template": _build_template(schema),
    }

    if output_dir is not None:
        validation = _write_validation_form(file_path, fields, schema, Path(output_dir))
        if validation is not None:
            result["validation_path"] = str(validation)

    return result


def _write_validation_form(
    file_path: Path,
    fields: list[dict],
    schema: dict,
    output_dir: Path,
) -> Path | None:
    """Produce a human-checkable artifact of the discovered field labels.

    For PDFs with geometry: draw each field's box and its label onto the
    rendered pages (a readable overlay - no truncation). Otherwise fall back
    to filling the form with each label.
    """
    output_dir.mkdir(parents=True, exist_ok=True)
    by_field_id = {p.get("x-field-id"): p for p in schema.get("properties", {}).values()}

    has_geometry = file_path.suffix.lower() == ".pdf" and any(
        f.get("rect") and f.get("page") is not None for f in fields
    )
    if has_geometry:
        return _write_validation_overlay(file_path, fields, by_field_id, output_dir)

    # Fallback: fill the form with each discovered label (may truncate).
    fill_data: dict[str, Any] = {}
    for field in fields:
        fid = field.get("name")
        prop = by_field_id.get(fid)
        if not fid or prop is None:
            continue
        if field.get("type") == "checkbox":
            fill_data[fid] = True
        else:
            label = prop.get("title") or ""
            if not label or label == fid:
                continue
            max_len = field.get("max_length")
            fill_data[fid] = label[:max_len] if max_len else label

    if not fill_data:
        return None
    out = output_dir / f"{file_path.stem}_validation{file_path.suffix}"
    try:
        fill_form(file_path, out, fill_data)
    except Exception:
        logger.warning("Failed to write validation form", exc_info=True)
        return None
    return out


def _write_validation_overlay(
    file_path: Path,
    fields: list[dict],
    by_field_id: dict[str, dict],
    output_dir: Path,
) -> Path | None:
    """Draw each field's box + discovered label onto the rendered pages.

    Resolved fields get a green box and their label; unresolved fields a red
    box. Pages are combined into a single ``*_validation.pdf``.
    """
    from PIL import Image, ImageDraw

    # Ensure codec plugins (JPEG/DCTDecode) are registered for PDF save.
    Image.init()

    work = output_dir / f"{file_path.stem}_valwork"
    meta = render_pdf_pages_meta(file_path, work, dpi=130)
    by_page: dict[int, list[dict]] = {}
    for f in fields:
        if f.get("rect") and f.get("page") is not None and not f.get("hidden"):
            by_page.setdefault(int(f["page"]), []).append(f)

    pages: list[Image.Image] = []
    for m in meta:
        img = Image.open(m["path"]).convert("RGB")
        draw = ImageDraw.Draw(img)
        for field in by_page.get(m["page"], []):
            prop = by_field_id.get(field.get("name"), {})
            title = prop.get("title", "")
            resolved = bool(title and title != field.get("name"))
            color = (0, 150, 0) if resolved else (220, 0, 0)
            px0, py0, px1, py1 = _rect_to_pixels(field["rect"], m["width_pt"], m["height_pt"], m["scale"])
            draw.rectangle([px0, py0, px1, py1], outline=color, width=2)
            text = title or field.get("name", "")
            if not text:
                continue
            box_h = py1 - py0
            box_w = px1 - px0
            if box_h >= 16 and box_w >= 40:
                # Large field: draw the label INSIDE, sized to the box.
                font = _load_font(max(10, min(18, box_h - 3)))
                fitted = _fit_text(draw, text, font, box_w - 4)
                if fitted:
                    draw.text((px0 + 2, py0 + 1), fitted, fill=color, font=font)
            else:
                # Small field/checkbox: no room inside - label sits ABOVE it,
                # on a tinted background so it stays readable.
                font = _load_font(13)
                fitted = _fit_text(draw, text, font, 220)
                tb = draw.textbbox((0, 0), fitted, font=font)
                tw, th = tb[2] - tb[0], tb[3] - tb[1]
                ty = py0 - (th + 4) if py0 > th + 4 else py1 + 1
                draw.rectangle([px0, ty, px0 + tw + 4, ty + th + 3], fill=(255, 255, 205))
                draw.text((px0 + 2, ty + 1), fitted, fill=color, font=font)
        pages.append(img)

    if not pages:
        shutil_rmtree(work)
        return None

    out = output_dir / f"{file_path.stem}_validation.pdf"
    pages[0].save(out, save_all=True, append_images=pages[1:])
    shutil_rmtree(work)
    return out


def shutil_rmtree(path: Path) -> None:
    import shutil

    shutil.rmtree(path, ignore_errors=True)


_FONT_CANDIDATES = ("DejaVuSans.ttf", "arial.ttf", "Arial.ttf", "LiberationSans-Regular.ttf")


def _load_font(size: int):
    """Load a TrueType font with full Latin/German glyph coverage.

    PIL's bitmap default font lacks umlauts/eszett (renders them as boxes),
    so prefer a Unicode TrueType face and fall back only if none is found.
    """
    from PIL import ImageFont

    for name in _FONT_CANDIDATES:
        try:
            return ImageFont.truetype(name, size)
        except OSError:
            continue
    try:
        return ImageFont.load_default(size=size)
    except TypeError:
        return ImageFont.load_default()


def _fit_text(draw, text: str, font, max_width: int) -> str:
    """Truncate text (with an ellipsis) so it fits within max_width pixels."""
    if max_width <= 0 or not text:
        return ""
    if draw.textlength(text, font=font) <= max_width:
        return text
    while text and draw.textlength(text + "…", font=font) > max_width:
        text = text[:-1]
    return (text + "…") if text else ""


def _build_review_client(settings: Settings):
    """Build the VLM review client (diagram_review, fallback codegen)."""
    from ..composition._generate_diagram import _build_client

    return _build_client(
        settings.diagram_review_endpoint or settings.diagram_codegen_endpoint,
        settings.diagram_review_api_key or settings.diagram_codegen_api_key,
        settings.diagram_review_model or settings.diagram_codegen_model,
        "diagram_review",
    )


_CROP_SYSTEM = """\
The image stacks several form-field regions, each under a black "FIELD N" \
header. Within each region exactly ONE field is outlined in RED, shown with \
its surrounding printed text for context. Your goal is to UNDERSTAND what each \
red-outlined field means. For each FIELD N report:
- description: a clear, self-contained explanation of what information this \
field captures, in the context of its section/row/column (1-2 sentences).
- label: the short printed caption for the box (for a checkbox, the option \
text beside it; for a table cell, the column header).
- type: the field kind if visible (text, checkbox, date, dropdown, ...)
- group: the section/heading the field belongs to.
- found: true if this is a real fillable field with an identifiable meaning; \
false if the red box is a row number, a code, a separator, or has no caption.

Rules:
- Judge each FIELD N only from its own region.
- For a segmented/comb input (a row of equal cells for one value, e.g. \
day | month | year), give the specific cell meaning (Tag, Monat, Jahr, ...).
- Read ONLY printed text. Use your judgement for what counts as a real label."""

_SOM_SYSTEM = """\
You are shown a FULL form page. A subset of fields is marked: each has a RED \
rectangle and a YELLOW numbered badge. You are given a PROPOSED understanding \
for each numbered field (from a close-up that lacked full-page context).

Using the WHOLE page - especially TABLE COLUMN HEADERS and section titles - \
return the correct understanding of each numbered field:
- description: a clear explanation of what this field captures, in context.
- label: its short caption / column header.
- type, group, found (false for row numbers, codes, separators, or boxes \
with no real caption).

Rules:
- Correct the proposal when it is wrong - most often table cells, whose true \
meaning is the COLUMN HEADER at the top of the column (possibly several rows \
above). Keep correct fine distinctions (date cells Tag/Monat/Jahr, checkbox \
options M/W/...); do NOT collapse them to the group header.
- Read ONLY printed text. Report every numbered field."""

_SOM_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "fields": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "number": {"type": "integer"},
                    "description": {"type": "string"},
                    "label": {"type": "string"},
                    "type": {"type": "string"},
                    "group": {"type": "string"},
                    "found": {"type": "boolean"},
                },
                "required": ["number", "found"],
            },
        }
    },
    "required": ["fields"],
}


def _rect_to_pixels(rect: list[float], width_pt: float, height_pt: float, scale: float) -> tuple[int, int, int, int]:
    """Map a PDF rect (points, bottom-left origin) to image pixels."""
    x0, y0, x1, y1 = rect
    px0 = int(min(x0, x1) * scale)
    px1 = int(max(x0, x1) * scale)
    # PDF y grows upward; image y grows downward.
    py0 = int((height_pt - max(y0, y1)) * scale)
    py1 = int((height_pt - min(y0, y1)) * scale)
    return px0, py0, px1, py1


def _sliding_windows(items: list, size: int, step: int) -> list[list]:
    """Split items into overlapping windows of up to `size`, advancing `step`."""
    if len(items) <= size:
        return [items]
    windows = []
    i = 0
    while i < len(items):
        windows.append(items[i : i + size])
        if i + size >= len(items):
            break
        i += step
    return windows


def _reading_order(fields: list[dict]) -> list[dict]:
    """Sort fields top-to-bottom then left-to-right (rough rows by y-band)."""
    return sorted(fields, key=lambda f: (-round(f["rect"][1] / 20), round(f["rect"][0] / 20)))


def _crop_field(page_img, meta: dict, field: dict):
    """Crop a context band around one field with the field outlined in red."""
    from PIL import ImageDraw

    px0, py0, px1, py1 = _rect_to_pixels(field["rect"], meta["width_pt"], meta["height_pt"], meta["scale"])
    if px1 - px0 < 6:
        px1 = px0 + 6
    if py1 - py0 < 6:
        py1 = py0 + 6
    w, h = page_img.size
    cx0 = max(0, px0 - 220)
    cy0 = max(0, py0 - 170)
    cx1 = min(w, px1 + 220)
    cy1 = min(h, py1 + 35)
    crop = page_img.crop((cx0, cy0, cx1, cy1)).copy()
    draw = ImageDraw.Draw(crop)
    draw.rectangle([px0 - cx0, py0 - cy0, px1 - cx0, py1 - cy0], outline=(220, 0, 0), width=3)
    return crop


def _build_montage(crops: list):
    """Stack field crops vertically, each under a black 'FIELD N' header."""
    from PIL import Image, ImageDraw

    font = _load_font(28)
    band = 40
    width = max(c.width for c in crops)
    total_h = sum(c.height + band + 8 for c in crops)
    montage = Image.new("RGB", (width, total_h), (255, 255, 255))
    draw = ImageDraw.Draw(montage)
    y = 0
    for i, c in enumerate(crops, 1):
        draw.rectangle([0, y, width, y + band], fill=(0, 0, 0))
        draw.text((8, y + 6), f"FIELD {i}", fill=(255, 255, 0), font=font)
        y += band
        montage.paste(c, (0, y))
        y += c.height + 8
    return montage


def _draw_window_marks(page_img, meta: dict, window: list[dict]):
    """Mark a window of fields on a copy of the FULL page (context kept).

    Returns (marked_full_page_image, {number: field_name}).
    """
    from PIL import ImageDraw

    img = page_img.copy()
    draw = ImageDraw.Draw(img)
    font = _load_font(26)

    num_to_fid: dict[int, str] = {}
    for i, field in enumerate(window, 1):
        num_to_fid[i] = field["name"]
        px0, py0, px1, py1 = _rect_to_pixels(field["rect"], meta["width_pt"], meta["height_pt"], meta["scale"])
        if px1 - px0 < 6:
            px1 = px0 + 6
        if py1 - py0 < 6:
            py1 = py0 + 6
        draw.rectangle([px0, py0, px1, py1], outline=(220, 0, 0), width=3)
        label = str(i)
        tb = draw.textbbox((0, 0), label, font=font)
        tw, th = tb[2] - tb[0], tb[3] - tb[1]
        bx0 = max(0, px0 - 1)
        by1 = max(th + 4, py0)
        by0 = by1 - (th + 4)
        draw.rectangle([bx0, by0, bx0 + tw + 6, by1], fill=(255, 215, 0))
        draw.text((bx0 + 3, by0 + 1), label, fill=(0, 0, 0), font=font)
    return img, num_to_fid


async def _som_resolve(
    file_path: Path,
    fields: list[dict],
    review_client,
    settings: Settings,
    *,
    max_iterations: int,
) -> dict[str, dict]:
    """Two-stage resolution: per-field crops, then a full-page review pass.

    Stage A (crops): each field is shown close-up with local context, so fine
    within-row labels (date cells Tag/Monat/Jahr, checkbox options) resolve
    accurately - but a crop can miss a table column's header.
    Stage B (full-page review): the whole page is shown with the proposed
    Stage-A labels; the VLM corrects table-column labels using the column
    headers it can now see, while keeping the correct fine labels.
    """
    from PIL import Image

    work_dir = Path(tempfile.mkdtemp(prefix="form_som_"))
    findings: dict[str, dict] = {}
    try:
        meta = render_pdf_pages_meta(file_path, work_dir, dpi=150, max_pages=settings.max_pdf_pages)
        page_meta = {m["page"]: m for m in meta}
        page_imgs = {m["page"]: Image.open(m["path"]).convert("RGB") for m in meta}

        # Resolve every visible (non-hidden) field with geometry; the LLM
        # decides per-field whether it is a real, meaningful field (found).
        all_targets = [f for f in fields if f.get("rect") and f.get("page") is not None and not f.get("hidden")]
        if not all_targets:
            return findings

        # Stage A: per-field crops (retried for stragglers up to max_iterations).
        prev_found = -1
        dry_streak = 0
        for iteration in range(1, max_iterations + 1):
            pending = [f for f in all_targets if not findings.get(f["name"], {}).get("found")]
            if not pending:
                break
            logger.info("Crop stage iteration %d: %d pending fields", iteration, len(pending))
            by_page: dict[int, list[dict]] = {}
            for f in pending:
                by_page.setdefault(int(f["page"]), []).append(f)
            for page_idx in sorted(by_page):
                m, page_img = page_meta.get(page_idx), page_imgs.get(page_idx)
                if m is None or page_img is None:
                    continue
                for batch in _chunk(_reading_order(by_page[page_idx]), 6):
                    crops = [_crop_field(page_img, m, f) for f in batch]
                    result = await _crop_call(crops, review_client)
                    num_to_fid = {i + 1: batch[i]["name"] for i in range(len(batch))}
                    for item in result:
                        fid = num_to_fid.get(item.get("number"))
                        if not fid:
                            continue
                        found = bool(item.get("found")) and bool(
                            (item.get("label") or item.get("description") or "").strip()
                        )
                        if found or fid not in findings:
                            findings[fid] = {
                                "label": (item.get("label") or "").strip(),
                                "type": item.get("type", ""),
                                "group": item.get("group", ""),
                                "description": (item.get("description") or "").strip(),
                                "found": found,
                            }
            found_now = sum(1 for v in findings.values() if v.get("found"))
            logger.info("Crop stage iteration %d: %d resolved", iteration, found_now)
            if found_now <= prev_found:
                dry_streak += 1
                if dry_streak >= _MAX_DRY_STREAK:
                    break
            else:
                dry_streak = 0
            prev_found = found_now

        # Stage B: full-page review corrects table-column labels.
        await _review_full_page(page_meta, page_imgs, all_targets, findings, review_client)

        # Co-located widgets (e.g. "1019" and "1019a" sharing a rect) cannot
        # be told apart by a numbered box. Inherit a resolved sibling's label.
        _propagate_colocated(all_targets, findings)
    finally:
        import shutil

        shutil.rmtree(work_dir, ignore_errors=True)

    return findings


async def _crop_call(crops: list, review_client) -> list[dict]:
    """Stage A: one structured VLM call over a montage of per-field crops."""
    import io

    montage = _build_montage(crops)
    buf = io.BytesIO()
    montage.save(buf, format="PNG")
    img_b64 = downscale_image_b64(base64.b64encode(buf.getvalue()).decode(), _IMAGE_B64_BUDGET)
    messages = [
        {"role": "system", "content": _CROP_SYSTEM},
        {"role": "user", "content": "Report the label for each FIELD N region (the red-outlined box)."},
    ]
    try:
        result = await review_client.chat_structured(
            messages, _SOM_SCHEMA, tool_name="form_fields", images_b64=[img_b64], max_tokens=8000
        )
    except Exception:
        logger.warning("Crop call failed", exc_info=True)
        return []
    return result.get("fields", [])


async def _review_full_page(page_meta, page_imgs, all_targets, findings, review_client) -> None:
    """Stage B: show the full page + proposed labels; correct table columns."""
    import io
    import json as _json

    by_page: dict[int, list[dict]] = {}
    for f in all_targets:
        by_page.setdefault(int(f["page"]), []).append(f)

    for page_idx in sorted(by_page):
        m, page_img = page_meta.get(page_idx), page_imgs.get(page_idx)
        if m is None or page_img is None:
            continue
        ordered = _reading_order(by_page[page_idx])
        for window in _sliding_windows(ordered, _WINDOW_SIZE, _WINDOW_STEP):
            marked, num_to_fid = _draw_window_marks(page_img, m, window)
            proposed = {
                i: {
                    "label": findings.get(num_to_fid[i], {}).get("label", ""),
                    "description": findings.get(num_to_fid[i], {}).get("description", ""),
                }
                for i in num_to_fid
            }
            buf = io.BytesIO()
            marked.save(buf, format="PNG")
            img_b64 = downscale_image_b64(base64.b64encode(buf.getvalue()).decode(), _PAGE_IMAGE_BUDGET)
            messages = [
                {"role": "system", "content": _SOM_SYSTEM},
                {
                    "role": "user",
                    "content": "Proposed understanding per numbered field:\n"
                    + _json.dumps(proposed, ensure_ascii=False),
                },
            ]
            try:
                result = (
                    await review_client.chat_structured(
                        messages, _SOM_SCHEMA, tool_name="form_fields", images_b64=[img_b64], max_tokens=8000
                    )
                ).get("fields", [])
            except Exception:
                logger.warning("Full-page review call failed", exc_info=True)
                continue
            for item in result:
                fid = num_to_fid.get(item.get("number"))
                if not fid:
                    continue
                label = (item.get("label") or "").strip()
                desc = (item.get("description") or "").strip()
                if bool(item.get("found")) and (label or desc):
                    cur = findings.get(fid, {})
                    findings[fid] = {
                        "label": label or cur.get("label", ""),
                        "type": item.get("type") or cur.get("type", ""),
                        "group": item.get("group") or cur.get("group", ""),
                        "description": desc or cur.get("description", ""),
                        "found": True,
                    }


def _propagate_colocated(targets: list[dict], findings: dict[str, dict]) -> None:
    """Give unresolved fields a resolved sibling's label when they overlap.

    Duplicate widgets (e.g. "1019" and "1019a") share a rect, so a numbered
    box cannot distinguish them. For each unresolved field, inherit the label
    of a resolved field on the same page whose rect is essentially the same.
    """

    def _key(field: dict) -> tuple:
        rect = field["rect"]
        return (field["page"], round(rect[0] / 3), round(rect[1] / 3), round(rect[2] / 3), round(rect[3] / 3))

    resolved_by_key: dict[tuple, dict] = {}
    for f in targets:
        finding = findings.get(f["name"])
        if finding and finding.get("found"):
            resolved_by_key.setdefault(_key(f), finding)

    for f in targets:
        if findings.get(f["name"], {}).get("found"):
            continue
        sibling = resolved_by_key.get(_key(f))
        if sibling:
            findings[f["name"]] = {**sibling, "inherited": True}


def _looks_human(text: str) -> bool:
    """True if text reads like a human label (a word/phrase, not an id)."""
    text = (text or "").strip()
    if not text:
        return False
    # A phrase with a space and some letters is human.
    if " " in text and _HUMAN_WORD_RE.search(text):
        return True
    # A single token is human only if it is a plain word with no digits,
    # brackets, dots, or underscores (e.g. "Familienname", not "f1_01[0]").
    if any(c in text for c in "[]().{}_/") or any(c.isdigit() for c in text):
        return False
    return bool(_HUMAN_WORD_RE.fullmatch(text))


def _is_cryptic(field: dict) -> bool:
    """True if the field lacks a human-meaningful label.

    Uses the /TU label when present; otherwise the last segment of the field
    name (XFA names like ``topmostSubform[0].Page1[0].f1_01[0]`` are cryptic).
    Hidden/no-view data-carrier fields never qualify - they do not print, so
    there is nothing to read visually.
    """
    if field.get("hidden"):
        return False
    name = field.get("name", "")
    label = field.get("label", "")
    if label and label != name and _looks_human(label):
        return False
    last_segment = name.split(".")[-1]
    return not _looks_human(last_segment)


_BASE36 = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ"


def _probe_token(counter: int) -> str:
    """Compact 3-char base36 code that stays unique in narrow form fields."""
    n = counter
    chars = []
    for _ in range(3):
        chars.append(_BASE36[n % 36])
        n //= 36
    return "".join(reversed(chars))


def _generate_probe_values(fields: list[dict]) -> dict[str, Any]:
    """Generate unique, type-aware probe values per field id.

    Text probes use compact 3-char codes (e.g. ``00A``) so they survive
    narrow/comb fields without clipping away their uniqueness.
    """
    probes: dict[str, Any] = {}
    counter = 0
    for field in fields:
        name = field.get("name")
        if not name:
            continue
        kind = field.get("type", "text")
        if kind == "checkbox":
            probes[name] = counter % 2 == 0
        elif kind in ("radio", "dropdown", "listbox", "combobox"):
            opts = field.get("options") or []
            if opts:
                probes[name] = opts[0]
        elif kind == "date":
            day = (counter % 28) + 1
            probes[name] = f"{day:02d}.02.2026"
        else:  # text, cell, unknown
            token = _probe_token(counter)
            max_len = field.get("max_length")
            if max_len and max_len < len(token):
                token = token[:max_len]
            probes[name] = token
        counter += 1
    return probes


async def _probe_and_review(
    file_path: Path,
    fields: list[dict],
    review_client,
    settings: Settings,
    *,
    max_iterations: int,
) -> dict[str, dict]:
    """Fill probe values, render, and ask the VLM to identify each field.

    Returns {field_id: finding} where finding has label/description/group/
    page/truncated/found.
    """
    work_dir = Path(tempfile.mkdtemp(prefix="form_visual_"))
    findings: dict[str, dict] = {}
    try:
        unresolved = [f for f in fields if _is_cryptic(f)]
        if not unresolved:
            logger.info("No cryptic fields - skipping visual inspection")
            return findings

        prev_found = -1
        dry_streak = 0
        for iteration in range(1, max_iterations + 1):
            pending = [f for f in unresolved if not findings.get(f["name"], {}).get("found")]
            if not pending:
                break
            logger.info("Visual inspect iteration %d: %d pending fields", iteration, len(pending))

            probes = _generate_probe_values(fields)
            filled = work_dir / f"filled_{iteration}{file_path.suffix}"
            fill_form(file_path, filled, probes)

            pages = render_document_pages(filled, work_dir / f"pages_{iteration}", max_pages=settings.max_pdf_pages)
            if not pages:
                logger.warning("No pages rendered, aborting visual inspection")
                break

            batch_findings = await _review_pages(pages, pending, probes, review_client)
            for fid, finding in batch_findings.items():
                if finding.get("found") or fid not in findings:
                    findings[fid] = finding

            found_now = sum(1 for v in findings.values() if v.get("found"))
            logger.info("Visual inspect iteration %d: %d fields resolved", iteration, found_now)
            if found_now <= prev_found:
                dry_streak += 1
                if dry_streak >= _MAX_DRY_STREAK:
                    break
            else:
                dry_streak = 0
            prev_found = found_now
    finally:
        import shutil

        shutil.rmtree(work_dir, ignore_errors=True)

    return findings


def _chunk(items: list, size: int) -> list[list]:
    return [items[i : i + size] for i in range(0, len(items), size)]


async def _review_pages(
    pages: list[Path],
    pending: list[dict],
    probes: dict[str, Any],
    review_client,
) -> dict[str, dict]:
    """Run the VLM one page at a time, capping fields per call.

    Sending too many fields against too many page images at once collapses
    recall, so each call gets a single page image and at most
    ``_FIELDS_PER_CALL`` field probes.
    """
    findings: dict[str, dict] = {}
    has_geometry = any(f.get("page") is not None for f in pending)

    if has_geometry:
        by_page: dict[int, list[dict]] = {}
        for field in pending:
            by_page.setdefault(int(field.get("page", 0)), []).append(field)
        for page_idx in sorted(by_page):
            if not (0 <= page_idx < len(pages)):
                continue
            img = pages[page_idx]
            for batch in _chunk(by_page[page_idx], _FIELDS_PER_CALL):
                findings.update(await _review_call([img], batch, probes, review_client))
    else:
        # No geometry (DOCX): try each page, dropping fields once found.
        remaining = list(pending)
        for img in pages:
            if not remaining:
                break
            page_findings: dict[str, dict] = {}
            for batch in _chunk(remaining, _FIELDS_PER_CALL):
                page_findings.update(await _review_call([img], batch, probes, review_client))
            findings.update(page_findings)
            found_ids = {fid for fid, v in page_findings.items() if v.get("found")}
            remaining = [f for f in remaining if f["name"] not in found_ids]

    return findings


async def _review_call(
    page_paths: list[Path],
    fields: list[dict],
    probes: dict[str, Any],
    review_client,
) -> dict[str, dict]:
    """One structured VLM call over a set of page images."""
    import json

    pairs = {f["name"]: probes.get(f["name"]) for f in fields if f["name"] in probes}
    if not pairs:
        return {}

    images_b64 = [downscale_image_b64(base64.b64encode(p.read_bytes()).decode(), _IMAGE_B64_BUDGET) for p in page_paths]

    messages = [
        {"role": "system", "content": _REVIEW_SYSTEM},
        {
            "role": "user",
            "content": (
                "Field probe values:\n"
                + json.dumps(pairs, ensure_ascii=False)
                + "\n\nIdentify each field from the pages."
            ),
        },
    ]
    try:
        result = await review_client.chat_structured(
            messages,
            _REVIEW_SCHEMA,
            tool_name="form_fields",
            images_b64=images_b64,
            max_tokens=8000,
        )
    except Exception:
        logger.warning("VLM review call failed", exc_info=True)
        return {}

    out: dict[str, dict] = {}
    for item in result.get("fields", []):
        fid = item.get("field_id")
        if fid:
            out[fid] = item
    return out


def _merge_findings(schema: dict, findings: dict[str, dict]) -> None:
    """Merge VLM findings into schema leaves, matched by x-field-id."""
    properties = schema.get("properties", {})
    by_field_id = {prop.get("x-field-id"): prop for prop in properties.values()}

    for fid, finding in findings.items():
        prop = by_field_id.get(fid)
        if prop is None or not finding.get("found"):
            continue
        description = finding.get("description", "")
        # The description (field meaning in context) is the primary output;
        # the title falls back to it when no short caption was found.
        title = finding.get("label") or description
        if title:
            prop["title"] = title
        if description:
            prop["description"] = description
        if finding.get("group"):
            prop["x-group"] = finding["group"]
        if finding.get("truncated"):
            prop["x-truncated"] = True


def _build_template(schema: dict) -> dict[str, str]:
    """Build a {speaking-label: field_id} template from the schema."""
    template: dict[str, str] = {}
    for prop in schema.get("properties", {}).values():
        fid = prop.get("x-field-id")
        title = prop.get("title")
        if fid and title:
            template[title] = fid
    return template
