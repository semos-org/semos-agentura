"""MCP + A2A service wrapper for document-agent.

Usage:
    uvicorn document_agent.service:app --port 8002
"""

from __future__ import annotations

import asyncio
import base64
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

from . import compose, digest, fill_form, generate_diagram, inspect_form
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
                description="Generate a diagram (Mermaid or draw.io) from a text description. Returns a download URL.",
                fn=self._generate_diagram,
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

    def _resolve_file(
        self,
        source: str,
        default_ext: str = ".bin",
        filename: str = "",
    ) -> Path:
        """Resolve source as a file path, base64, or data URI.

        If filename is provided, the temp file preserves that name
        (important for downstream tools that infer type from name).
        """
        p = Path(source)
        if p.exists():
            return p

        raw = source
        if raw.startswith("data:"):
            _, encoded = raw.split(",", 1)
            raw = encoded
        try:
            data = base64.b64decode(raw, validate=True)
            if len(data) > 4:
                if filename:
                    subdir = self.output_dir / f"_att_{uuid.uuid4().hex[:8]}"
                    subdir.mkdir(exist_ok=True)
                    tmp = subdir / filename
                else:
                    tmp = self.output_dir / f"_upload_{uuid.uuid4().hex[:8]}{default_ext}"
                tmp.write_bytes(data)
                return tmp
        except Exception:
            pass
        return p

    def _resolve_file_attachment(
        self,
        source: FileAttachment | str,
        default_ext: str = ".bin",
    ) -> Path:
        """Resolve a FileAttachment or plain string to a local Path."""
        if isinstance(source, dict):
            name = source.get("name", "")
            content = source.get("content", name)
            ext = Path(name).suffix if name else default_ext
            return self._resolve_file(content, default_ext=ext, filename=name)
        return self._resolve_file(source, default_ext=default_ext)

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
        src = self._resolve_file_attachment(source, ".pdf")
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
        filename: str = "",
        reference_doc: FileAttachment | str = "",
        header_footer_doc: FileAttachment | str = "",
    ) -> str:
        """Render Markdown into a document. Source is a file path or raw Markdown text.

        Footnotes ([^1]: text), comments ({.comment-start/end} spans),
        and tracked changes ({.insertion}/{.deletion} spans) from
        digest_document output are reproduced in the output DOCX.

        Styles can be defined in three ways (in priority order):
        1. YAML front matter in the Markdown source (auto-generates a
           reference doc):
             ---
             styles:
               page: {size: "A4", margin-top: "1.5cm", ...}
               body: {font: "Calibri", size: 11, line-spacing: 1.1,
                      spacing-before: "0.0cm", spacing-after: "0.1cm"}
               heading1: {font: "Calibri", size: 13, bold: true,
                          color: "000080", spacing-before: "0.3cm"}
               heading2: {font: "Calibri", size: 11, bold: true, ...}
               heading3: {font: "Calibri", size: 11, bold: true, italic: true}
               table: {size: 9, border-color: "999999", border-size: 4}
             ---
           Table size also controls footnote and caption font size.
           Combine with header_footer_doc to also get headers/footers.
        2. reference_doc parameter: a DOCX/ODT file whose styles, headers,
           footers, and page layout are all applied (overrides YAML styles).
        3. Neither: pandoc default styles.

        Args:
            source: Path to a .md file, or raw Markdown content.
            format: Output format - 'pdf', 'pptx', 'docx', or 'html'.
            is_slides: Set to true for slide/presentation output.
            filename: Optional output filename. Auto-generated if omitted.
            reference_doc: Optional DOCX/ODT file whose styles, headers,
                footers, and page layout are all applied.
            header_footer_doc: Optional DOCX to copy only headers and footers
                from. Use with YAML styles to get custom fonts/spacing plus
                template headers/footers.
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
            ref_path = self._resolve_file_attachment(reference_doc, ".docx")

        # Resolve header/footer source if provided
        hf_path = None
        if header_footer_doc:
            hf_path = self._resolve_file_attachment(header_footer_doc, ".docx")

        def _run():
            return compose(
                source=source_path,
                output_path=out_path,
                format=fmt,
                is_slides=is_slides,
                reference_doc=ref_path,
                header_footer_doc=hf_path,
                settings=self._settings,
            )

        result = await asyncio.to_thread(_run)
        return self.file_response(result.output_path, display_name=filename)

    async def _generate_diagram(self, description: str, diagram_type: str = "mermaid") -> str:
        """Generate a diagram from a text description."""
        # generate_diagram is async (unlike the other functions)
        result = await generate_diagram(
            description=description,
            diagram_type=diagram_type,
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
        fp = self._resolve_file_attachment(file_path, ".pdf")

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
        fp = self._resolve_file_attachment(file_path, ".pdf")
        if not filename:
            ext = fp.suffix or ".pdf"
            filename = f"filled{ext}"
        safe_name = f"{uuid.uuid4().hex[:8]}_{filename}"
        out_path = self.output_dir / safe_name

        def _run():
            return fill_form(file_path=fp, output_path=out_path, data=field_data)

        result_path = await asyncio.to_thread(_run)
        return self.file_response(Path(result_path), display_name=filename)


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
