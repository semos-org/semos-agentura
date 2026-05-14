"""MCP + A2A service wrapper for document-agent.

Usage:
    uvicorn document_agent.service:app --port 8002
"""

from __future__ import annotations

import json
import os
from pathlib import Path

from dotenv import load_dotenv

# Load .env from agent dir, then workspace root
_agent_dir = Path(__file__).resolve().parent.parent.parent
load_dotenv(_agent_dir / ".env")
load_dotenv(_agent_dir.parent / ".env")

from agentura_commons import (
    BaseAgentService,
    SkillDef,
    create_app,
)

from .config import Settings


class DocumentAgentService(BaseAgentService):
    """Exposes document-agent's tools via MCP and skills via A2A."""

    def __init__(self) -> None:
        self._settings = Settings()

    @property
    def agent_name(self) -> str:
        return "Document Agent"

    @property
    def agent_description(self) -> str:
        return "Document processing - digest (OCR), compose (render), generate diagrams and images, and fill forms."

    @property
    def agent_version(self) -> str:
        return "0.1.0"

    def get_tools(self) -> list:
        from .tools import get_document_tools

        return get_document_tools(self)

    def get_skills(self) -> list[SkillDef]:
        return [
            SkillDef(
                id="document-processing",
                name="Document Processing",
                description="Digest, compose, diagram generation, and form operations on documents.",
                tags=["document", "ocr", "pdf", "diagram"],
            ),
        ]

    async def execute_skill(self, skill_id: str, message: str, *, task_id: str | None = None) -> str:
        msg = message.lower()
        if "digest" in msg or "ocr" in msg:
            return "Use the digest_document tool with a file path to extract content from a document."
        elif "compose" in msg or "render" in msg:
            return "Use the compose_document tool with Markdown content and an output format."
        elif "diagram" in msg:
            return "Use the generate_diagram tool with a text description."
        elif "image" in msg or "icon" in msg or "raster" in msg:
            return "Use the generate_image tool with a description and mode (generate/edit/cut)."
        elif "form" in msg and "fill" in msg:
            return "Use the fill_form tool with a file path and field data."
        elif "form" in msg and "inspect" in msg:
            return "Use the inspect_form tool with a file path."
        return (
            "Available tools: digest_document (supports DOCX tracked changes, footnotes, comments), "
            "compose_document (supports footnotes and reference doc for styles), "
            "generate_diagram, generate_image, inspect_form, fill_form."
        )

    async def _get_examples(self) -> str:
        """Return reference Markdown examples for all composition tools."""
        return json.dumps(
            {
                "document": _EXAMPLE_DOCUMENT,
                "marp_slides": _EXAMPLE_MARP_SLIDES,
                "pandoc_slides": _EXAMPLE_PANDOC_SLIDES,
                "description": (
                    "Three Markdown formats supported by compose_document:\n"
                    "1. 'document': General documents (PDF, DOCX) with inline "
                    "mermaid/drawio diagrams, YAML styles, footnotes.\n"
                    "2. 'marp_slides': Marp presentations (is_slides=true). "
                    "Polished output. Use draft=true for editable PPTX.\n"
                    "3. 'pandoc_slides': Native pandoc slide markdown "
                    "(is_slides=true, draft=true). Always fully editable."
                ),
            },
            ensure_ascii=False,
        )


# ------------------------------------------------------------------
# Reference examples for get_examples tool
# ------------------------------------------------------------------

_EXAMPLE_DOCUMENT = """\
---
title: "Document Title"
subtitle: "Optional Subtitle"
styles:
  page:
    size: "A4"
    margin-top: "1.5cm"
    margin-bottom: "1.5cm"
    margin-left: "2cm"
    margin-right: "2cm"
  body:
    font: "Calibri"
    size: 11
    line-spacing: 1.15
  heading1:
    font: "Calibri"
    size: 14
    bold: true
    color: "003366"
  heading2:
    size: 12
    bold: true
  table:
    size: 9
    border-color: "999999"
---

# Introduction

This is a general document with **rich formatting** support.

## Inline Diagrams

Mermaid code blocks are auto-rendered to images:

```mermaid
graph LR
    A[Input] --> B[Process]
    B --> C[Output]
```

## Tables

| Feature    | Status    |
|------------|-----------|
| Footnotes  | Supported |
| Comments   | Supported |
| Styles     | YAML      |

## Footnotes

This text has a footnote[^1].

[^1]: Footnote content here.

## Images

![Description](path/to/image.png)

Inline base64 images are also supported and auto-extracted to files.
"""

_EXAMPLE_MARP_SLIDES = """\
---
marp: true
theme: default
paginate: true
style: |
  section { font-size: 22px; }
  h1 { color: #2c3e50; }
---

<!-- _class: title -->
# Presentation Title
## Author Name

Conference | Date

<!-- Speaker notes go here.
They appear in presenter view. -->

---

# Content Slide

- Bullet point one
- Bullet point two
- **Bold** and *italic* supported

---

# Split Layout

![bg right:48% fit](image.jpg)

Text appears on the left side.

- Works with any image
- Percentage controls split

---

# Image Slide

![w:800](diagram.png)

Caption text below the image.

---

# Table Slide

| Column 1 | Column 2 | Column 3 |
|----------|----------|----------|
| Data     | More     | Info     |

Key finding described below the table.

---

# Two Columns (HTML)

<div class="columns">
<div>

### Left Column
- Point A
- Point B

</div>
<div>

### Right Column
- Point X
- Point Y

</div>
</div>
"""

_EXAMPLE_PANDOC_SLIDES = """\
---
title: Presentation Title
author: Author Name
date: 2026-01-01
---

# Section Title

## Slide with Bullets

- This is a basic slide with bullet points
- It uses the "Title and Content" layout
- Perfect for simple content

::: notes
Speaker notes for this slide.
Remember to emphasize point 2.
:::

## Two Column Layout

::::: columns
::: column
Left column content:

- Point 1
- Point 2
:::
::: column
Right column content:

- Point A
- Point B
:::
:::::

## Comparison with Image

::::: columns
::: column
Explanatory text about the image.

Key observations:

- Finding one
- Finding two
:::
::: column
![Caption](image.jpg)
:::
:::::

## Table Example

| Column 1 | Column 2 | Column 3 |
|----------|----------|----------|
| Row 1    | Data     | More     |
| Row 2    | Info     | Stuff    |

## Code Block

```python
def greet(name):
    return f"Hello, {name}!"
```

## Incremental List

::: incremental
- This point appears first
- Then this one
- And finally this one
:::

# Conclusion

## Thank You

Thank you for viewing this presentation!

::: notes
Invite questions from the audience.
:::
"""


# --- App factory ---
_service = DocumentAgentService()


def create_service_app(
    host: str | None = None,
    port: str | int | None = None,
):
    """Create the FastAPI app. Called lazily by uvicorn."""
    h = host or os.getenv("AGENT_HOST", "127.0.0.1")
    p = port or os.getenv("AGENT_PORT", "8002")
    return create_app(_service, base_url=f"http://{h}:{p}")


app = create_service_app()
