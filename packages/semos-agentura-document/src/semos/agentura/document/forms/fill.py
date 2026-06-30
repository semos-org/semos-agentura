"""Unified form filling and inspection for PDF and DOCX."""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from ..exceptions import DocumentAgentError
from ._docx_forms import fill_docx_fields, inspect_docx_fields
from ._pdf_forms import fill_pdf_fields, inspect_pdf_fields
from ._schema import fields_to_data, fields_to_schema, flatten_to_field_ids

logger = logging.getLogger(__name__)


def inspect_form_fields(file_path: Path | str) -> list[dict]:
    """Return the raw list of field metadata dicts for a PDF or DOCX.

    Lower-level than ``inspect_form``: callers that need per-field geometry,
    flags, or options (e.g. the visual inspect loop) use this. Each dict has
    at least: name, type, value; PDF adds label/page/rect/max_length/flags.
    """
    file_path = Path(file_path)
    ext = file_path.suffix.lower()

    if ext == ".pdf":
        return inspect_pdf_fields(file_path)
    elif ext in (".docx", ".doc"):
        return inspect_docx_fields(file_path)
    else:
        raise DocumentAgentError(f"Unsupported form file type: {ext}")


def inspect_form(file_path: Path | str) -> dict:
    """Inspect form fields in a PDF or DOCX file.

    Returns ``{"schema": <JSON Schema>, "data": {field_id: current_value}}``.
    The schema describes every field (type, label, options, max_length, page,
    flags) with each leaf carrying ``x-field-id`` = the real fill key. The
    data dict is a round-trip-ready fill input of current values.
    """
    fields = inspect_form_fields(file_path)
    return {"schema": fields_to_schema(fields), "data": fields_to_data(fields)}


def fill_form(
    file_path: Path | str,
    output_path: Path | str,
    data: dict[str, Any] | Path | str,
) -> Path:
    """Fill form fields in a PDF or DOCX file.

    Args:
        file_path: Source file with form fields.
        output_path: Where to write the filled file.
        data: Field values as a dict, or path to a JSON file containing the dict.
            Keys are field names, values depend on field type:
            - text/date: str
            - checkbox: bool
            - radio: str (option value)
            - dropdown: str (display value)

    Returns:
        Path to the output file.
    """
    file_path = Path(file_path)
    output_path = Path(output_path)

    # Load data from JSON file if needed
    if isinstance(data, (str, Path)) and not isinstance(data, dict):
        data_path = Path(data)
        if data_path.exists() and data_path.suffix == ".json":
            data = json.loads(data_path.read_text(encoding="utf-8"))
        elif isinstance(data, str):
            # Try parsing as inline JSON
            try:
                data = json.loads(data)
            except json.JSONDecodeError as e:
                raise DocumentAgentError(f"Could not parse data as JSON: {data[:100]}") from e

    if not isinstance(data, dict):
        raise DocumentAgentError(f"Data must be a dict or path to JSON file, got {type(data)}")

    # Accept a {schema, data} pair (from inspect_form) - flatten via x-field-id
    # to the {field_id: value} dict the fillers expect.
    if "schema" in data and "data" in data and isinstance(data["schema"], dict):
        data = flatten_to_field_ids(data["schema"], data["data"])

    ext = file_path.suffix.lower()
    if ext == ".pdf":
        return fill_pdf_fields(file_path, output_path, data)
    elif ext in (".docx", ".doc"):
        return fill_docx_fields(file_path, output_path, data)
    else:
        raise DocumentAgentError(f"Unsupported form file type: {ext}")


def _load_json(source: dict | Path | str) -> dict:
    """Load a dict from a dict, JSON file path, or JSON string."""
    if isinstance(source, dict):
        return source
    p = Path(source)
    if p.exists() and p.suffix == ".json":
        return json.loads(p.read_text(encoding="utf-8"))
    if isinstance(source, str):
        return json.loads(source)
    raise DocumentAgentError(f"Cannot load JSON from {source}")


def fill_form_with_template(
    file_path: Path | str,
    output_path: Path | str,
    data: dict[str, Any] | Path | str,
    template: dict | Path | str,
) -> Path:
    """Fill form fields using semantic names translated via a template.

    Args:
        file_path: Source file with form fields.
        output_path: Where to write the filled file.
        data: Semantic field values as dict or JSON file/string.
        template: Template with a "fields" mapping of semantic name -> internal field name.
            Can be a dict, path to JSON file, or JSON string.

    Returns:
        Path to the output file.
    """
    data = _load_json(data) if not isinstance(data, dict) else data
    tmpl = _load_json(template)

    field_map = tmpl.get("fields", {})
    if not field_map:
        raise DocumentAgentError("Template has no 'fields' mapping")

    # Translate semantic keys to internal field names
    translated: dict[str, Any] = {}
    for semantic_key, value in data.items():
        internal_key = field_map.get(semantic_key)
        if internal_key:
            translated[internal_key] = value
        else:
            # Pass through keys not in template (might be direct field names)
            translated[semantic_key] = value
            logger.debug("Key '%s' not in template, passing through as-is", semantic_key)

    return fill_form(file_path, output_path, translated)
