"""MCP + A2A service wrapper for document-agent.

Usage:
    uvicorn document_agent.service:app --port 8002
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import uuid
from pathlib import Path

from dotenv import load_dotenv

# Load .env from agent dir, then workspace root
_agent_dir = Path(__file__).resolve().parent.parent.parent
load_dotenv(_agent_dir / ".env")
load_dotenv(_agent_dir.parent / ".env")

from agentura_commons import (
    BaseAgentService,
    FileAttachment,
    SkillDef,
    ToolDef,
    create_app,
)

from . import (
    MergeConfig,
    compose,
    digest,
    fill_form,
    generate_diagram,
    inspect_form,
    merge_slides,
)
from .config import Settings
from .models import OutputFormat, OutputMode


class DocumentAgentService(BaseAgentService):
    """Exposes document-agent's tools via MCP and skills via A2A."""

    def __init__(self) -> None:
        self._settings = Settings()

    @property
    def agent_name(self) -> str:
        return "Document Agent"

    @property
    def agent_description(self) -> str:
        return "Document processing - digest (OCR), compose (render), generate diagrams, and fill forms."

    @property
    def agent_version(self) -> str:
        return "0.1.0"

    def get_tools(self) -> list[ToolDef]:
        _fh = "Accepts an absolute file path or base64-encoded file content."
        return [
            ToolDef(
                name="digest_document",
                description=(
                    "Digest a document (PDF, image, Office) into Markdown. "
                    "DOCX/ODT use pandoc and preserve: "
                    "(1) footnotes as [^N] / [^N]: text, "
                    "(2) tracked changes as {.insertion}/{.deletion} spans with author+date, "
                    "(3) comments as {.comment-start id='N' author='X' date='Y'}...{.comment-end id='N'}, "
                    "(4) document styles as YAML front matter (fonts, sizes, colors, margins). "
                    "All four round-trip through compose_document. "
                    "Use track_changes='all' to see revisions, 'accept' for final text, "
                    "'reject' for original text. "
                    f"PDF/images use OCR. {_fh}"
                ),
                fn=self._digest,
                file_params=["source"],
                read_only=True,
                idempotent=True,
            ),
            ToolDef(
                name="compose_document",
                description=(
                    "Render Markdown source text into a document (PDF, PPTX, DOCX, HTML). "
                    "Footnotes, comments, and tracked changes from digest_document "
                    "round-trip back to DOCX. "
                    "Styles can be controlled via YAML front matter in the Markdown: "
                    "styles.page (size, margins), styles.body (font, size, line-spacing, "
                    "spacing-before/after), styles.heading1/2/3 (font, size, bold, italic, "
                    "color, spacing), styles.table (size, border-color, border-size). "
                    "Footnotes and captions use table.size (default 9pt). "
                    "If no YAML styles are present, an optional reference_doc DOCX "
                    "can be provided for style inheritance. Returns a download URL."
                ),
                fn=self._compose,
                file_params=["reference_doc", "header_footer_doc"],
                task_support="optional",
                idempotent=True,
            ),
            ToolDef(
                name="generate_diagram",
                description="Generate or modify a diagram (Mermaid or draw.io). "
                "Pass 'source' to modify an existing diagram (file path, drawio/mermaid code, "
                "or an image to redraw). Returns a download URL.",
                fn=self._generate_diagram,
                file_params=["source"],
                task_support="optional",
            ),
            ToolDef(
                name="inspect_form",
                description=f"Inspect form fields in a PDF or DOCX. {_fh}",
                fn=self._inspect_form,
                file_params=["file_path"],
                read_only=True,
                idempotent=True,
            ),
            ToolDef(
                name="fill_form",
                description=f"Fill form fields in a PDF or DOCX. Returns a download URL. {_fh}",
                fn=self._fill_form,
                file_params=["file_path"],
                task_support="optional",
            ),
            ToolDef(
                name="merge_slides",
                description=(
                    "Merge slides from multiple PPTX files into one. "
                    "Pass each PPTX as a separate file parameter "
                    "(file1, file2, ... up to file5). "
                    "Use slides1, slides2, etc. to specify which slides "
                    "to include (0-based indices as comma-separated "
                    "string, e.g. '0,1,2'). Omit for all slides. "
                    "The first file's theme is used."
                ),
                fn=self._merge_slides,
                file_params=[
                    "file1",
                    "file2",
                    "file3",
                    "file4",
                    "file5",
                ],
                task_support="optional",
            ),
            ToolDef(
                name="get_examples",
                description=(
                    "Get reference Markdown examples for all document-agent composition tools. "
                    "Returns examples for: general documents (with inline diagrams/images), "
                    "Marp slides, and pandoc slides. Use these as templates when composing."
                ),
                fn=self._get_examples,
                read_only=True,
                idempotent=True,
            ),
        ]

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
        elif "form" in msg and "fill" in msg:
            return "Use the fill_form tool with a file path and field data."
        elif "form" in msg and "inspect" in msg:
            return "Use the inspect_form tool with a file path."
        return (
            "Available tools: digest_document (supports DOCX tracked changes, footnotes, comments), "
            "compose_document (supports footnotes and reference doc for styles), "
            "generate_diagram, inspect_form, fill_form."
        )

    # _resolve_file and _resolve_file_attachment inherited from BaseAgentService

    async def _digest(
        self,
        source: FileAttachment | str,
        output_mode: str = "text",
        max_pages: int | None = None,
        digest_mode: str = "auto",
        track_changes: str = "accept",
        describe_images: bool = False,
    ) -> str:
        """Digest a document into Markdown.

        DOCX/ODT files are processed via pandoc, preserving:
        - Footnotes as [^N] with [^N]: definition at end
        - Tracked changes as {.insertion author="X" date="Y"} /
          {.deletion author="X" date="Y"} spans (when track_changes='all')
        - Comments as {.comment-start id="N" author="X" date="Y"}...
          {.comment-end id="N"} spans
        - Document styles as YAML front matter block (page size/margins,
          body/heading fonts/sizes/colors/spacing, table properties)

        All four round-trip through compose_document back to DOCX.

        Args:
            source: File as {name, content} object, file path, or base64.
            output_mode: 'text' for inline markdown, 'file' to write to disk.
            max_pages: Maximum number of pages to process.
            digest_mode: 'auto' (pandoc for DOCX/ODT, OCR otherwise),
                'ocr' (force OCR), or 'pandoc' (force pandoc).
            track_changes: 'accept' (final text, default), 'reject'
                (original text), or 'all' (both with author/date annotations).
            describe_images: Send extracted images to VLM for alt-text annotation.
        """
        mode = OutputMode.INLINE if output_mode == "text" else OutputMode.FILE
        src = self.resolve_file_attachment(source, ".pdf")
        settings = self._settings

        def _run():
            return digest(
                source=src,
                output_mode=mode,
                max_pages=max_pages,
                digest_mode=digest_mode,
                track_changes=track_changes,
                describe_images=describe_images,
                settings=settings,
            )

        result = await asyncio.to_thread(_run)
        return json.dumps({"markdown": result.markdown or ""}, ensure_ascii=False)

    async def _compose(
        self,
        source: str,
        format: str,
        is_slides: bool = False,
        draft: bool = False,
        template: str = "",
        template_backend: str = "auto",
        filename: str = "",
        reference_doc: FileAttachment | str = "",
        header_footer_doc: FileAttachment | str = "",
    ) -> str:
        """Render Markdown into a document. Source is a file path or raw Markdown text.

        For slides (is_slides=True), two modes are available:
        - draft=False (default): Marp renders polished slides with limited
          editability via --pptx-editable. Best visual quality.
        - draft=True: pandoc renders fully editable PPTX. Rougher layout
          but every element is editable in PowerPoint. Auto-detects Marp
          vs plain pandoc markdown (Marp has 'marp: true' in frontmatter).

        For draft slides, an optional template PPTX can be applied to add
        corporate branding (requires PowerPoint COM on Windows).

        Use get_examples tool to see reference Markdown for each format.

        For documents (is_slides=False), styles can be defined via:
        1. YAML front matter (auto-generates reference doc)
        2. reference_doc parameter (DOCX/ODT template)
        3. Pandoc defaults

        Args:
            source: Path to a .md file, or raw Markdown content.
            format: Output format - 'pdf', 'pptx', 'docx', or 'html'.
            is_slides: Set to true for slide/presentation output.
            draft: For slides: fully editable PPTX via pandoc (rough layout).
            template: For draft slides: PPTX template for corporate branding.
            template_backend: Template backend: auto, com, uno, docker.
            filename: Optional output filename. Auto-generated if omitted.
            reference_doc: DOCX/ODT/PPTX for style inheritance.
            header_footer_doc: DOCX to copy only headers/footers from.
        """
        fmt = OutputFormat(format)
        if not filename:
            filename = f"output.{fmt.value}"
        safe_name = f"{uuid.uuid4().hex[:8]}_{filename}"
        out_path = self.output_dir / safe_name

        # If source looks like a path to an existing file, use it directly.
        # Otherwise treat it as raw Markdown content and write a temp file.
        source_path = Path(source)
        if not source_path.exists():
            tmp_md = self.output_dir / f"_source_{filename}.md"
            tmp_md.write_text(source, encoding="utf-8")
            source_path = tmp_md

        # Resolve reference document if provided
        ref_path = None
        if reference_doc:
            ref_path = self.resolve_file_attachment(reference_doc, ".docx")

        # Resolve header/footer source if provided
        hf_path = None
        if header_footer_doc:
            hf_path = self.resolve_file_attachment(header_footer_doc, ".docx")

        tpl_path = Path(template) if template else None

        def _run():
            return compose(
                source=source_path,
                output_path=out_path,
                format=fmt,
                is_slides=is_slides,
                draft=draft,
                template=tpl_path,
                template_backend=template_backend,
                reference_doc=ref_path,
                header_footer_doc=hf_path,
                settings=self._settings,
            )

        result = await asyncio.to_thread(_run)
        return self.file_response(result.output_path, display_name=filename)

    async def _generate_diagram(
        self,
        description: str = "",
        diagram_type: str = "mermaid",
        source: FileAttachment | str | None = None,
    ) -> str:
        """Generate or modify a diagram.

        Args:
            description: Natural-language description or modification instructions.
            diagram_type: 'mermaid' or 'drawio'.
            source: Existing diagram to modify - file path, inline code,
                or image to redraw. Accepts {name, content} file attachment.
        """
        src = None
        if source:
            src = self.resolve_file_attachment(source, ".png")
        result = await generate_diagram(
            description=description or None,
            diagram_type=diagram_type,
            source=src,
            output_dir=self.output_dir,
            settings=self._settings,
        )
        resp: dict = {
            "iterations": result.iterations,
        }
        if result.image_path:
            img = Path(result.image_path)
            safe_name = f"{uuid.uuid4().hex[:8]}_{img.name}"
            dest = self.output_dir / safe_name
            shutil.copy2(img, dest)
            file_meta = json.loads(self.file_response(dest, display_name=img.name))
            resp.update(file_meta)
        return json.dumps(resp, ensure_ascii=False)

    async def _inspect_form(self, file_path: FileAttachment | str) -> str:
        """Inspect form fields in a PDF or DOCX.

        Args:
            file_path: File as {name, content} object, file path, or base64.
        """
        fp = self.resolve_file_attachment(file_path, ".pdf")

        def _run():
            return inspect_form(file_path=fp)

        fields = await asyncio.to_thread(_run)
        return json.dumps(fields, ensure_ascii=False)

    async def _fill_form(
        self,
        file_path: FileAttachment | str,
        data: str,
        filename: str = "",
    ) -> str:
        """Fill form fields and return a download URL.

        Args:
            file_path: File as {name, content} object, file path, or base64.
            data: JSON string of {field_name: value} pairs.
            filename: Optional output filename. Auto-generated if omitted.
        """
        field_data = json.loads(data)
        fp = self.resolve_file_attachment(file_path, ".pdf")
        if not filename:
            ext = fp.suffix or ".pdf"
            filename = f"filled{ext}"
        safe_name = f"{uuid.uuid4().hex[:8]}_{filename}"
        out_path = self.output_dir / safe_name

        def _run():
            return fill_form(file_path=fp, output_path=out_path, data=field_data)

        result_path = await asyncio.to_thread(_run)
        return self.file_response(Path(result_path), display_name=filename)

    async def _merge_slides(
        self,
        file1: FileAttachment | str,
        file2: FileAttachment | str = "",
        file3: FileAttachment | str = "",
        file4: FileAttachment | str = "",
        file5: FileAttachment | str = "",
        slides1: str = "",
        slides2: str = "",
        slides3: str = "",
        slides4: str = "",
        slides5: str = "",
        output_filename: str = "merged.pptx",
        backend: str = "auto",
    ) -> str:
        """Merge slides from multiple PPTX files.

        Each file parameter accepts a file path or attachment.
        The matching slides parameter is a comma-separated list
        of 0-based slide indices (e.g. "0,1,2"). Omit or leave
        empty to include all slides from that file.
        The first file's theme/master is used for the output.

        Args:
            file1: First PPTX file (required).
            file2..file5: Additional PPTX files (optional).
            slides1..slides5: Slide indices for each file.
            output_filename: Name for the output file.
            backend: 'auto', 'com', or 'pptx'.
        """
        from .composition._slide_merge import SlideRef

        files = [file1, file2, file3, file4, file5]
        slide_specs = [slides1, slides2, slides3, slides4, slides5]

        src_map: dict[str, str] = {}
        slide_refs: list[SlideRef] = []

        for raw_file, spec in zip(files, slide_specs, strict=True):
            if not raw_file:
                continue
            resolved = self.resolve_file_attachment(raw_file, ".pptx")
            if not resolved.exists():
                continue
            name = resolved.stem
            if name in src_map:
                i = 2
                while f"{name}_{i}" in src_map:
                    i += 1
                name = f"{name}_{i}"
            src_map[name] = str(resolved.resolve())

            if spec and spec.strip():
                for idx in spec.split(","):
                    idx = idx.strip()
                    if idx.isdigit():
                        slide_refs.append(SlideRef(source=name, index=int(idx)))
            else:
                slide_refs.append(SlideRef(source=name, index=-1))

        base_path = next(iter(src_map.values()))
        config = MergeConfig(
            base=str(Path(base_path).resolve()),
            sources=src_map,
            slides=slide_refs,
        )

        safe = f"{uuid.uuid4().hex[:8]}_{output_filename}"
        out = self.output_dir / safe

        def _run():
            return merge_slides(config, out, backend=backend)

        result = await asyncio.to_thread(_run)
        return self.file_response(result.output_path, display_name=output_filename)

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
