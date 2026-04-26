"""Document agent: digestion, composition, forms, diagrams.

Provides OCR-to-Markdown, Markdown-to-documents, slide
composition, diagram generation, and form filling.
"""

from .composition import (
    MergeConfig,
    apply_template,
    compose,
    compose_editable_slides,
    generate_diagram,
    load_merge_config,
    merge_slides,
    parse_source_args,
)
from .config import Settings
from .digestion import digest
from .forms import fill_form, fill_form_with_template, inspect_form
from .models import (
    ComposeResult,
    DiagramResult,
    DigestResult,
    MergeResult,
    OutputFormat,
    OutputMode,
)

__all__ = [
    "ComposeResult",
    "DiagramResult",
    "DigestResult",
    "MergeConfig",
    "MergeResult",
    "OutputFormat",
    "OutputMode",
    "Settings",
    "apply_template",
    "compose",
    "compose_editable_slides",
    "digest",
    "fill_form",
    "fill_form_with_template",
    "generate_diagram",
    "inspect_form",
    "load_merge_config",
    "merge_slides",
    "parse_source_args",
]
