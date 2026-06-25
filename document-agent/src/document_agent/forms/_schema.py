"""Form field schema: convert inspect field dicts to JSON Schema + data.

The schema is a standard JSON Schema (type: object, properties: {...}) whose
property keys are user/LLM-facing names. Each leaf carries ``x-field-id`` =
the real (often cryptic) form fill key, so speaking names or nested objects
can be introduced without losing the link used to fill.

A separate ``data`` object mirrors the schema shape with current values.
``flatten_to_field_ids`` walks both and resolves leaves back to the flat
{field_id: value} dict that the PDF/DOCX fillers consume.
"""

from __future__ import annotations

from typing import Any

# Widget kinds that map to a JSON Schema boolean.
_BOOLEAN_TYPES = {"checkbox"}
# Widget kinds that carry an option list (enum).
_ENUM_TYPES = {"radio", "dropdown", "listbox", "combobox"}


def field_to_property(field: dict[str, Any]) -> dict[str, Any]:
    """Convert one inspect field dict into a JSON Schema leaf property."""
    kind = field.get("type", "text")

    prop: dict[str, Any] = {}
    if kind in _BOOLEAN_TYPES:
        prop["type"] = "boolean"
    else:
        prop["type"] = "string"
        if kind == "date":
            prop["format"] = "date"

    label = field.get("label") or field.get("name", "")
    if label:
        prop["title"] = label

    options = field.get("options")
    if options and kind in _ENUM_TYPES:
        prop["enum"] = options

    max_length = field.get("max_length")
    if max_length:
        prop["maxLength"] = max_length

    if field.get("description"):
        prop["description"] = field["description"]

    # Current value as the inline default.
    value = field.get("value")
    if value is None:
        value = field.get("default")
    if value is not None and value != "":
        prop["default"] = value

    flags = field.get("flags") or []
    if "readonly" in flags:
        prop["readOnly"] = True

    # Form-specific extensions.
    prop["x-field-id"] = field.get("name", "")
    prop["x-field-type"] = kind
    for src, dst in (("page", "x-page"), ("rect", "x-rect"), ("group", "x-group")):
        if field.get(src) is not None:
            prop[dst] = field[src]
    if flags:
        prop["x-flags"] = flags

    return prop


def fields_to_schema(fields: list[dict[str, Any]]) -> dict[str, Any]:
    """Build a JSON Schema object from a list of inspect field dicts.

    Property keys start equal to the field id (``name``). Duplicate or empty
    names are disambiguated so every field gets a stable property key.
    """
    properties: dict[str, Any] = {}
    required: list[str] = []
    for i, field in enumerate(fields):
        key = field.get("name") or f"field_{i}"
        if key in properties:
            suffix = 2
            while f"{key}__{suffix}" in properties:
                suffix += 1
            key = f"{key}__{suffix}"
        properties[key] = field_to_property(field)
        if "required" in (field.get("flags") or []):
            required.append(key)

    schema: dict[str, Any] = {"type": "object", "properties": properties}
    if required:
        schema["required"] = required
    return schema


def fields_to_data(fields: list[dict[str, Any]]) -> dict[str, Any]:
    """Build a flat {field_id: current_value} dict from inspect fields."""
    data: dict[str, Any] = {}
    for field in fields:
        name = field.get("name")
        if not name:
            continue
        value = field.get("value")
        if value is None or value == "":
            continue
        data[name] = value
    return data


def flatten_to_field_ids(schema: dict[str, Any], data: dict[str, Any]) -> dict[str, Any]:
    """Resolve a (possibly nested) data object to a flat {field_id: value} dict.

    Walks schema.properties and the matching data values in parallel. Object
    properties recurse; leaf properties read ``x-field-id`` to find the real
    fill key. Data keys with no schema entry are passed through unchanged
    (assumed to already be field ids).
    """
    flat: dict[str, Any] = {}
    properties = schema.get("properties", {})

    for key, value in data.items():
        prop = properties.get(key)
        if prop is None:
            # No schema entry - assume it is already a field id.
            flat[key] = value
            continue
        if prop.get("type") == "object" and isinstance(value, dict):
            flat.update(flatten_to_field_ids(prop, value))
            continue
        field_id = prop.get("x-field-id", key)
        flat[field_id] = value

    return flat


def build_data_from_values(schema: dict[str, Any], values: dict[str, Any]) -> dict[str, Any]:
    """Build a schema-shaped (possibly nested) data object from flat values.

    Inverse of ``flatten_to_field_ids``: for each schema property, look up its
    ``x-field-id`` in ``values`` and place the value under the property key,
    recursing into nested objects.
    """
    data: dict[str, Any] = {}
    properties = schema.get("properties", {})

    for key, prop in properties.items():
        if prop.get("type") == "object":
            nested = build_data_from_values(prop, values)
            if nested:
                data[key] = nested
            continue
        field_id = prop.get("x-field-id", key)
        if field_id in values:
            data[key] = values[field_id]

    return data
