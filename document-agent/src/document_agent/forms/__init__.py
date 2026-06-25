from ._schema import build_data_from_values, fields_to_data, fields_to_schema, flatten_to_field_ids
from ._visual_inspect import inspect_form_visual
from .fill import fill_form, fill_form_with_template, inspect_form, inspect_form_fields

__all__ = [
    "fill_form",
    "fill_form_with_template",
    "inspect_form",
    "inspect_form_fields",
    "inspect_form_visual",
    "fields_to_schema",
    "fields_to_data",
    "flatten_to_field_ids",
    "build_data_from_values",
]
