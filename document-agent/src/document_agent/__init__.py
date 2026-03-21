"""Document digestion (OCR to Markdown) and composition (Markdown to documents) agent."""

from .composition import compose, generate_diagram
from .config import Settings
from .digestion import digest
from .forms import fill_form, fill_form_with_template, inspect_form
from .models import ComposeResult, DiagramResult, DigestResult, OutputFormat, OutputMode

__all__ = [
    "ComposeResult",
    "DiagramResult",
    "DigestResult",
    "OutputFormat",
    "OutputMode",
    "Settings",
    "compose",
    "digest",
    "fill_form",
    "fill_form_with_template",
    "generate_diagram",
    "inspect_form",
]
