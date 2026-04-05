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

from agentura_commons import (
    BaseAgentService, FileAttachment, SkillDef, ToolDef, create_app,
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
            ToolDef(name="digest_document", description=f"Digest a document (PDF, image, Office) into Markdown via OCR. {_fh}", fn=self._digest, file_params=["source"], read_only=True, idempotent=True),
            ToolDef(name="compose_document", description="Render Markdown source text into a document (PDF, PPTX, DOCX, HTML). Returns a download URL.", fn=self._compose, task_support="optional", idempotent=True),
            ToolDef(name="generate_diagram", description="Generate a diagram (Mermaid or draw.io) from a text description. Returns a download URL.", fn=self._generate_diagram, task_support="optional"),
            ToolDef(name="inspect_form", description=f"Inspect form fields in a PDF or DOCX. {_fh}", fn=self._inspect_form, file_params=["file_path"], read_only=True, idempotent=True),
            ToolDef(name="fill_form", description=f"Fill form fields in a PDF or DOCX. Returns a download URL. {_fh}", fn=self._fill_form, file_params=["file_path"], task_support="optional"),
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
        return "Available tools: digest_document, compose_document, generate_diagram, inspect_form, fill_form."

    def _resolve_file(
        self, source: str,
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
        self, source: FileAttachment | str,
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
        self, source: FileAttachment | str,
        output_mode: str = "text",
        max_pages: int | None = None,
    ) -> str:
        """Digest a document into Markdown.

        Args:
            source: File as {name, content} object, file path, or base64.
            output_mode: 'text' for inline markdown, 'file' to write to disk.
            max_pages: Maximum number of pages to process.
        """
        mode = OutputMode.INLINE if output_mode == "text" else OutputMode.FILE
        src = self._resolve_file_attachment(source, ".pdf")
        settings = self._settings

        def _run():
            return digest(source=src, output_mode=mode, max_pages=max_pages, settings=settings)

        result = await asyncio.to_thread(_run)
        return json.dumps({"markdown": result.markdown or ""}, ensure_ascii=False)

    async def _compose(self, source: str, format: str, is_slides: bool = False, filename: str = "") -> str:
        """Render Markdown into a document. Source is a file path or raw Markdown text.

        Args:
            source: Path to a .md file, or raw Markdown content.
            format: Output format - 'pdf', 'pptx', 'docx', or 'html'.
            is_slides: Set to true for slide/presentation output.
            filename: Optional output filename. Auto-generated if omitted.
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

        def _run():
            return compose(source=source_path, output_path=out_path, format=fmt, is_slides=is_slides, settings=self._settings)

        result = await asyncio.to_thread(_run)
        return self.file_response(result.output_path, display_name=filename)

    async def _generate_diagram(self, description: str, diagram_type: str = "mermaid") -> str:
        """Generate a diagram from a text description."""
        # generate_diagram is async (unlike the other functions)
        result = await generate_diagram(
            description=description, diagram_type=diagram_type,
            output_dir=self.output_dir, settings=self._settings,
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
        self, file_path: FileAttachment | str,
        data: str, filename: str = "",
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
    host: str | None = None, port: str | int | None = None,
):
    """Create the FastAPI app. Called lazily by uvicorn."""
    h = host or os.getenv("AGENT_HOST", "127.0.0.1")
    p = port or os.getenv("AGENT_PORT", "8002")
    return create_app(_service, base_url=f"http://{h}:{p}")


app = create_service_app()
