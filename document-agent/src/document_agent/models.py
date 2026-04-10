from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path

from pydantic import BaseModel
from pydantic import Field as PydanticField


class OutputFormat(str, Enum):
    MARKDOWN = "markdown"
    HTML = "html"
    PDF = "pdf"
    PPTX = "pptx"
    DOCX = "docx"
    ODT = "odt"


class OutputMode(str, Enum):
    FILE = "file"
    INLINE = "inline"


class ImageDescription(BaseModel):
    """Built-in schema for VLM-based image annotation."""

    image_type: str = PydanticField(
        ...,
        description="Type of image: e.g. photo, diagram, chart, table, map, screenshot, handwriting, logo",
    )
    text_content: str = PydanticField(
        ...,
        description="All text visible in the image, transcribed verbatim",
    )
    description: str = PydanticField(
        ...,
        description="Detailed description of the image content and layout",
    )


@dataclass
class DigestResult:
    """Result of document digestion."""

    markdown: str
    output_path: Path | None = None
    images_dir: Path | None = None
    annotation: dict | None = None
    annotation_path: Path | None = None


@dataclass
class ComposeResult:
    """Result of document composition."""

    output_path: Path | None = None
    content: bytes | str | None = None
    format: OutputFormat = OutputFormat.PDF


@dataclass
class DiagramResult:
    """Result of diagram generation with optimization."""

    code: str
    image_path: Path | None
    iterations: int
    review_log: list[dict] = field(default_factory=list)
