"""PDF form filling and inspection via pypdf."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from pypdf import PdfReader, PdfWriter

logger = logging.getLogger(__name__)

# PDF field type constants
FT_TEXT = "/Tx"
FT_BUTTON = "/Btn"  # checkbox, radio
FT_CHOICE = "/Ch"  # dropdown, listbox
FT_SIGNATURE = "/Sig"

# Field flag bits (from PDF spec)
FF_PUSHBUTTON = 1 << 16
FF_RADIO = 1 << 15

# Common /Ff flag bits mapped to readable names (1-based bit numbers from spec).
_FLAG_BITS = {
    "readonly": 1 << 0,
    "required": 1 << 1,
    "no-export": 1 << 2,
    "multiline": 1 << 12,  # text fields
    "password": 1 << 13,  # text fields
    "comb": 1 << 24,  # text fields
}

# Text justification (/Q) values.
_ALIGN = {0: "left", 1: "center", 2: "right"}


def inspect_pdf_fields(file_path: Path) -> list[dict]:
    """Inspect all form fields in a PDF. Returns a list of field metadata dicts.

    Each dict carries every available AcroForm annotation: name, type, current
    value/default, label (/TU tooltip), max_length (/MaxLen), alignment (/Q),
    decoded flags (/Ff), options, and the widget's page index and rect.
    """
    reader = PdfReader(file_path)
    fields = reader.get_fields()
    if not fields:
        return []

    geometry = _build_geometry_map(reader)

    result = []
    for name, field in fields.items():
        ft = str(field.get("/FT", ""))
        ff = int(field.get("/Ff", 0))
        value = field.get("/V")
        default = field.get("/DV")

        kind = _classify_field(ft, ff)

        entry: dict[str, Any] = {
            "name": name,
            "type": kind,
            "pdf_type": ft,
            "value": _format_value(value),
            "default": _format_value(default),
        }

        # Human label from the tooltip / alternate field name.
        tu = field.get("/TU")
        if tu:
            entry["label"] = str(tu)

        # Capacity and presentation.
        max_len = field.get("/MaxLen")
        if max_len is not None:
            try:
                entry["max_length"] = int(max_len)
            except (TypeError, ValueError):
                pass
        q = field.get("/Q")
        if q is not None and int(q) in _ALIGN:
            entry["alignment"] = _ALIGN[int(q)]

        flags = _decode_flags(ff)
        if flags:
            entry["flags"] = flags

        # Geometry (page index + rect) from the widget annotation.
        geo = geometry.get(name)
        if geo:
            entry["page"] = geo[0]
            entry["rect"] = geo[1]
            if geo[2]:
                entry["hidden"] = True
                entry.setdefault("flags", []).append("hidden")

        # For radio/dropdown, extract options
        if kind == "radio":
            entry["options"] = _extract_radio_options(field)
        elif kind in ("dropdown", "listbox"):
            entry["options"] = _extract_choice_options(field)

        result.append(entry)

    return result


def _decode_flags(ff: int) -> list[str]:
    """Decode the /Ff bitmask into readable flag names."""
    return [name for name, bit in _FLAG_BITS.items() if ff & bit]


def _build_geometry_map(reader: PdfReader) -> dict[str, tuple[int, list[float], bool]]:
    """Map field name -> (page_index, rect, hidden) from widget annotations.

    The merged get_fields() dict lacks geometry, so we traverse each page's
    /Annots and match widgets to their fully-qualified field name via /T,
    walking /Parent for nested names. ``hidden`` is true for widgets whose
    annotation /F flag sets Hidden (bit 2) or NoView (bit 6) - data-carrier
    fields that do not print and so cannot be labeled visually.
    """
    geometry: dict[str, tuple[int, list[float], bool]] = {}
    for page_index, page in enumerate(reader.pages):
        annots = page.get("/Annots")
        if not annots:
            continue
        for annot_ref in annots:
            try:
                annot = annot_ref.get_object()
            except Exception:
                continue
            name = _qualified_name(annot)
            if not name or name in geometry:
                continue
            rect = annot.get("/Rect")
            if rect is None:
                continue
            flags = int(annot.get("/F", 0))
            hidden = bool(flags & 2 or flags & 32)
            try:
                geometry[name] = (page_index, [float(x) for x in rect], hidden)
            except (TypeError, ValueError):
                continue
    return geometry


def _qualified_name(annot: Any) -> str | None:
    """Build the fully-qualified field name for a widget annotation."""
    parts: list[str] = []
    node: Any = annot
    seen = 0
    while node is not None and seen < 20:
        t = node.get("/T")
        if t:
            parts.append(str(t))
        parent = node.get("/Parent")
        node = parent.get_object() if parent is not None and hasattr(parent, "get_object") else None
        seen += 1
    if not parts:
        return None
    return ".".join(reversed(parts))


def fill_pdf_fields(file_path: Path, output_path: Path, data: dict[str, Any]) -> Path:
    """Fill form fields in a PDF and write to output_path.

    Args:
        file_path: Source PDF with form fields.
        output_path: Where to write the filled PDF.
        data: Mapping of field name -> value.
            - Text fields: str
            - Checkboxes: bool
            - Radio buttons: str (the option value, e.g. "/Auswahl1")
            - Dropdowns: str
    """
    reader = PdfReader(file_path)
    writer = PdfWriter()
    writer.append(reader)

    fields = reader.get_fields() or {}
    filled_count = 0

    for field_name, value in data.items():
        if field_name not in fields:
            logger.warning("Field '%s' not found in PDF, skipping", field_name)
            continue

        field = fields[field_name]
        ft = str(field.get("/FT", ""))
        ff = int(field.get("/Ff", 0))
        kind = _classify_field(ft, ff)

        try:
            if kind == "text":
                writer.update_page_form_field_values(None, {field_name: str(value)})
                filled_count += 1
            elif kind == "checkbox":
                _fill_checkbox(writer, field_name, bool(value))
                filled_count += 1
            elif kind == "radio":
                writer.update_page_form_field_values(None, {field_name: str(value)})
                filled_count += 1
            elif kind in ("dropdown", "listbox"):
                writer.update_page_form_field_values(None, {field_name: str(value)})
                filled_count += 1
            else:
                logger.debug("Skipping unsupported field type '%s' for '%s'", kind, field_name)
        except Exception as e:
            logger.warning("Failed to fill field '%s': %s", field_name, e)

    output_path.parent.mkdir(parents=True, exist_ok=True)
    with open(output_path, "wb") as f:
        writer.write(f)

    logger.info("Filled %d/%d fields, written: %s", filled_count, len(data), output_path)
    return output_path


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _classify_field(ft: str, ff: int) -> str:
    if ft == FT_TEXT:
        return "text"
    if ft == FT_BUTTON:
        if ff & FF_PUSHBUTTON:
            return "button"
        if ff & FF_RADIO:
            return "radio"
        return "checkbox"
    if ft == FT_CHOICE:
        # Bit 17 = combo box; otherwise list box
        if ff & (1 << 17):
            return "dropdown"
        return "listbox"
    if ft == FT_SIGNATURE:
        return "signature"
    return ft or "unknown"


def _format_value(v: Any) -> Any:
    if v is None:
        return None
    return str(v)


def _extract_radio_options(field: dict) -> list[str]:
    kids = field.get("/Kids", [])
    options = set()
    for kid in kids:
        kid_obj = kid.get_object() if hasattr(kid, "get_object") else kid
        ap = kid_obj.get("/AP", {})
        n = ap.get("/N", {})
        if hasattr(n, "keys"):
            for key in n.keys():
                if key != "/Off":
                    options.add(str(key))
    return sorted(options)


def _extract_choice_options(field: dict) -> list[str]:
    opt = field.get("/Opt", [])
    return [str(o) for o in opt]


def _fill_checkbox(writer: PdfWriter, field_name: str, checked: bool) -> None:
    """Fill a checkbox field across all pages."""
    from pypdf.generic import NameObject

    for page in writer.pages:
        annots = page.get("/Annots", [])
        for annot_ref in annots:
            annot = annot_ref.get_object() if hasattr(annot_ref, "get_object") else annot_ref
            t = annot.get("/T")
            if t and str(t) == field_name:
                # Determine the "on" value from the appearance dict
                ap = annot.get("/AP", {})
                n = ap.get("/N", {})
                on_value = NameObject("/Yes")
                if hasattr(n, "keys"):
                    for key in n.keys():
                        if str(key) != "/Off":
                            on_value = NameObject(str(key))
                            break
                val = on_value if checked else NameObject("/Off")
                annot[NameObject("/V")] = val
                annot[NameObject("/AS")] = val
