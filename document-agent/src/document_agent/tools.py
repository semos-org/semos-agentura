"""AgentTool definitions for document-agent.

Each tool has a Pydantic input model for validation + schema generation,
and an AgentTool subclass with the async implementation.
"""

from __future__ import annotations

import asyncio
import json
import shutil
import uuid
from enum import Enum
from pathlib import Path
from typing import Any

from agentura_commons import AgentTool, FileAttachment, NamedFile, ToolResult
from pydantic import BaseModel, Field

from . import (
    MergeConfig,
    compose,
    digest,
    fill_form,
    generate_diagram,
    generate_image_fn,
    inspect_form,
    merge_slides,
)
from .models import OutputFormat, OutputMode

# Input enums


class DigestOutputMode(str, Enum):
    TEXT = "text"
    FILE = "file"


class DigestMode(str, Enum):
    AUTO = "auto"
    OCR = "ocr"
    PANDOC = "pandoc"


class TrackChanges(str, Enum):
    ACCEPT = "accept"
    REJECT = "reject"
    ALL = "all"


# Input models


class DigestInput(BaseModel):
    source: FileAttachment | str = Field(
        description="Document to digest (file path, base64, or data URI). Supports PDF, images, Office formats.",
        json_schema_extra={"x-file": True},
    )
    output_mode: DigestOutputMode = Field(
        default=DigestOutputMode.FILE,
        description=(
            "'text' returns markdown inline (best for small docs without images). "
            "'file' writes .md + images to disk (best for docs with images or large content). "
            "Auto-switches to 'file' when images are present or content exceeds 50 KB."
        ),
    )
    max_pages: int | None = Field(
        default=None,
        description="Maximum number of pages to OCR.",
    )
    digest_mode: DigestMode = Field(
        default=DigestMode.AUTO,
        description="'auto' (pandoc for DOCX/ODT, OCR otherwise), 'ocr', or 'pandoc'.",
    )
    track_changes: TrackChanges = Field(
        default=TrackChanges.ACCEPT,
        description="'accept' (final text), 'reject' (original), or 'all' (with author/date).",
    )
    describe_images: bool = Field(
        default=False,
        description="Send extracted images to VLM for alt-text annotation.",
    )


class ComposeInput(BaseModel):
    source: str = Field(
        description="Path to a .md file, or raw Markdown content.",
    )
    format: str = Field(
        description="Output format: 'pdf', 'pptx', 'docx', or 'html'.",
    )
    is_slides: bool = Field(
        default=False,
        description="True for slide/presentation output.",
    )
    draft: bool = Field(
        default=False,
        description="For slides: fully editable PPTX via pandoc (rough layout).",
    )
    template: str = Field(
        default="",
        description="For draft slides: PPTX template path for corporate branding.",
    )
    template_backend: str = Field(
        default="auto",
        description="Template backend: auto, com, uno, docker.",
    )
    filename: str = Field(
        default="",
        description="Output filename. Auto-generated if omitted.",
    )
    reference_doc: FileAttachment | str = Field(
        default="",
        description="DOCX/ODT/PPTX for style inheritance.",
        json_schema_extra={"x-file": True},
    )
    header_footer_doc: FileAttachment | str = Field(
        default="",
        description="DOCX to copy only headers/footers from.",
        json_schema_extra={"x-file": True},
    )


class EmbedItem(BaseModel):
    """A raster image to embed in a diagram."""

    name: str = Field(description="Filename of the image (e.g. 'logo.png').")
    content: str = Field(
        default="",
        description="File content as base64/path/data URI. Uses name as path if empty.",
        json_schema_extra={"x-file": True},
    )
    description: str = Field(
        default="",
        description="Where/how to place this image in the diagram.",
    )


class GenerateDiagramInput(BaseModel):
    description: str = Field(
        default="",
        description="Natural-language description or modification instructions.",
    )
    diagram_type: str = Field(
        default="mermaid",
        description="'mermaid' or 'drawio'.",
    )
    source: FileAttachment | str | None = Field(
        default=None,
        description="Existing diagram to modify (file path, code, or image).",
        json_schema_extra={"x-file": True},
    )
    embeds: list[EmbedItem] | None = Field(
        default=None,
        description="Raster images to embed in the diagram (icons, logos, symbols).",
    )


class GenerateImageInput(BaseModel):
    description: str = Field(
        description="What to generate/edit/extract.",
    )
    mode: str = Field(
        default="generate",
        description="'generate' (text-to-image), 'edit' (modify), or 'cut' (extract element).",
    )
    source: FileAttachment | str | None = Field(
        default=None,
        description="Input image for edit/cut modes.",
        json_schema_extra={"x-file": True},
    )
    mask: FileAttachment | str | None = Field(
        default=None,
        description="Mask image for inpainting (white = edit area).",
        json_schema_extra={"x-file": True},
    )
    style: str = Field(
        default="",
        description="Style hint (e.g. 'flat icon', 'photorealistic').",
    )
    size: str = Field(
        default="1024x1024",
        description="Output size (e.g. '1024x1024', '512x512').",
    )
    background: str = Field(
        default="auto",
        description="'auto', 'transparent', or 'white'.",
    )
    output_format: str = Field(
        default="png",
        description="'png' or 'webp'.",
    )


class InspectFormInput(BaseModel):
    file_path: FileAttachment | str = Field(
        description="PDF or DOCX to inspect (file path, base64, or data URI).",
        json_schema_extra={"x-file": True},
    )


class FillFormInput(BaseModel):
    file_path: FileAttachment | str = Field(
        description="PDF or DOCX to fill (file path, base64, or data URI).",
        json_schema_extra={"x-file": True},
    )
    data: str = Field(
        description="JSON string of {field_name: value} pairs.",
    )
    filename: str = Field(
        default="",
        description="Output filename. Auto-generated if omitted.",
    )


class MergeSlidesInput(BaseModel):
    file1: FileAttachment | str = Field(
        description="First PPTX file (required).",
        json_schema_extra={"x-file": True},
    )
    file2: FileAttachment | str = Field(
        default="",
        description="Second PPTX file.",
        json_schema_extra={"x-file": True},
    )
    file3: FileAttachment | str = Field(
        default="",
        description="Third PPTX file.",
        json_schema_extra={"x-file": True},
    )
    file4: FileAttachment | str = Field(
        default="",
        description="Fourth PPTX file.",
        json_schema_extra={"x-file": True},
    )
    file5: FileAttachment | str = Field(
        default="",
        description="Fifth PPTX file.",
        json_schema_extra={"x-file": True},
    )
    slides1: str = Field(
        default="",
        description="Slide indices for file1 (0-based, comma-separated).",
    )
    slides2: str = Field(default="", description="Slide indices for file2.")
    slides3: str = Field(default="", description="Slide indices for file3.")
    slides4: str = Field(default="", description="Slide indices for file4.")
    slides5: str = Field(default="", description="Slide indices for file5.")
    output_filename: str = Field(
        default="merged.pptx",
        description="Name for the output file.",
    )
    backend: str = Field(
        default="auto",
        description="'auto', 'com', or 'pptx'.",
    )


class GetExamplesInput(BaseModel):
    pass


# Tool implementations


class DigestDocumentTool(AgentTool):
    name: str = "digest_document"
    description: str = (
        "Digest a document (PDF, image, Office) into Markdown. "
        "DOCX/ODT use pandoc and preserve: "
        "(1) footnotes, (2) tracked changes with author/date, "
        "(3) comments with author/date, (4) document styles as YAML front matter. "
        "All four round-trip through compose_document. "
        "Use track_changes='all' to see revisions. "
        "PDF/images use OCR."
    )
    args_schema: type[BaseModel] = DigestInput
    read_only: bool = True
    idempotent: bool = True

    async def _arun(self, **kwargs: Any) -> ToolResult | str:
        svc = self._service
        output_mode = kwargs.get("output_mode", DigestOutputMode.FILE)
        src = svc.resolve_file_attachment(kwargs["source"], ".pdf")

        # Always run FILE mode internally so images land on disk
        def _run():
            return digest(
                source=src,
                output_mode=OutputMode.FILE,
                max_pages=kwargs.get("max_pages"),
                digest_mode=DigestMode(kwargs.get("digest_mode", DigestMode.AUTO)).value,
                track_changes=TrackChanges(kwargs.get("track_changes", TrackChanges.ACCEPT)).value,
                describe_images=kwargs.get("describe_images", False),
                settings=svc._settings,
            )

        result = await asyncio.to_thread(_run)
        md = result.markdown or ""

        # Collect extracted image files with relative subdirectory names
        image_files: list[NamedFile] = []
        if result.images_dir and result.images_dir.exists():
            dir_name = result.images_dir.name
            for img in sorted(result.images_dir.iterdir()):
                image_files.append(NamedFile(path=img, name=f"{dir_name}/{img.name}"))

        # Auto-switch to file mode when content has images or is large
        _MAX_INLINE_BYTES = 50_000
        use_file = output_mode == DigestOutputMode.FILE or image_files or len(md.encode()) > _MAX_INLINE_BYTES

        if use_file:
            files: list[Path | NamedFile] = []
            if result.output_path:
                files.append(result.output_path)
            files.extend(image_files)
            return ToolResult(text=md, files=files)

        return md


class ComposeDocumentTool(AgentTool):
    name: str = "compose_document"
    description: str = (
        "Render Markdown source text into a document (PDF, PPTX, DOCX, HTML). "
        "Footnotes, comments, and tracked changes round-trip back to DOCX. "
        "Styles via YAML front matter or reference_doc. Returns a download URL."
    )
    args_schema: type[BaseModel] = ComposeInput
    task_support: str | None = "optional"
    idempotent: bool = True

    async def _arun(self, **kwargs: Any) -> str:
        svc = self._service
        fmt = OutputFormat(kwargs["format"])
        filename = kwargs.get("filename") or f"output.{fmt.value}"
        safe_name = f"{uuid.uuid4().hex[:8]}_{filename}"
        out_path = svc.output_dir / safe_name

        source = kwargs["source"]
        source_path = Path(source)
        if not source_path.exists():
            tmp_md = svc.output_dir / f"_source_{filename}.md"
            tmp_md.write_text(source, encoding="utf-8")
            source_path = tmp_md

        ref_path = None
        if kwargs.get("reference_doc"):
            ref_path = svc.resolve_file_attachment(kwargs["reference_doc"], ".docx")

        hf_path = None
        if kwargs.get("header_footer_doc"):
            hf_path = svc.resolve_file_attachment(kwargs["header_footer_doc"], ".docx")

        tpl_path = Path(kwargs["template"]) if kwargs.get("template") else None

        def _run():
            return compose(
                source=source_path,
                output_path=out_path,
                format=fmt,
                is_slides=kwargs.get("is_slides", False),
                draft=kwargs.get("draft", False),
                template=tpl_path,
                template_backend=kwargs.get("template_backend", "auto"),
                reference_doc=ref_path,
                header_footer_doc=hf_path,
                settings=svc._settings,
            )

        result = await asyncio.to_thread(_run)
        return NamedFile(path=result.output_path, name=filename)


class GenerateDiagramTool(AgentTool):
    name: str = "generate_diagram"
    description: str = (
        "Generate or modify a diagram (Mermaid or draw.io). "
        "Pass 'source' to modify an existing diagram. "
        "Pass 'embeds' to include raster images. Returns a download URL."
    )
    args_schema: type[BaseModel] = GenerateDiagramInput
    task_support: str | None = "optional"

    async def _arun(self, **kwargs: Any) -> str:
        svc = self._service
        src = None
        if kwargs.get("source"):
            src = svc.resolve_file_attachment(kwargs["source"], ".png")

        resolved_embeds = None
        if kwargs.get("embeds"):
            embeds_raw = kwargs["embeds"]
            # LLM may pass a plain string instead of a list
            if isinstance(embeds_raw, str):
                embeds_raw = [{"name": embeds_raw}]
            resolved_embeds = []
            for embed in embeds_raw:
                # Handle EmbedItem (Pydantic model), dict, or plain string
                if hasattr(embed, "model_dump"):
                    embed = embed.model_dump()
                if isinstance(embed, str):
                    embed = {"name": embed}
                if isinstance(embed, dict):
                    name = embed.get("name", "")
                    content = embed.get("content", "") or name
                    desc = embed.get("description", "") or name or Path(content).stem
                    ext = Path(name).suffix if name else ".png"
                    path = svc.resolve_file(content, default_ext=ext, filename=name)
                    resolved_embeds.append({"path": path, "description": desc})

        result = await generate_diagram(
            description=kwargs.get("description") or None,
            diagram_type=kwargs.get("diagram_type", "mermaid"),
            source=src,
            embeds=resolved_embeds,
            output_dir=svc.output_dir,
            settings=svc._settings,
        )
        resp: dict = {"iterations": result.iterations}
        files: list = []
        if result.image_path:
            img = Path(result.image_path)
            safe_name = f"{uuid.uuid4().hex[:8]}_{img.name}"
            dest = svc.output_dir / safe_name
            shutil.copy2(img, dest)
            files.append(NamedFile(path=dest, name=img.name))
        return ToolResult(data=resp, files=files)


class GenerateImageTool(AgentTool):
    name: str = "generate_image"
    description: str = (
        "Generate, edit, or cut elements from raster images. Modes: 'generate', 'edit', 'cut'. Returns a download URL."
    )
    args_schema: type[BaseModel] = GenerateImageInput
    task_support: str | None = "optional"

    async def _arun(self, **kwargs: Any) -> str:
        svc = self._service
        src_path = None
        if kwargs.get("source"):
            src_path = svc.resolve_file_attachment(kwargs["source"], ".png")
        mask_path = None
        if kwargs.get("mask"):
            mask_path = svc.resolve_file_attachment(kwargs["mask"], ".png")

        result = await generate_image_fn(
            description=kwargs["description"],
            mode=kwargs.get("mode", "generate"),
            source=src_path,
            mask=mask_path,
            style=kwargs.get("style", ""),
            size=kwargs.get("size", "1024x1024"),
            background=kwargs.get("background", "auto"),
            output_format=kwargs.get("output_format", "png"),
            output_dir=svc.output_dir,
            settings=svc._settings,
        )
        img = result.image_path
        safe_name = f"{uuid.uuid4().hex[:8]}_{img.name}"
        dest = svc.output_dir / safe_name
        shutil.copy2(img, dest)
        return ToolResult(
            data={"mode": result.mode, "size": list(result.size)},
            files=[NamedFile(path=dest, name=img.name)],
        )


class InspectFormTool(AgentTool):
    name: str = "inspect_form"
    description: str = "Inspect form fields in a PDF or DOCX."
    args_schema: type[BaseModel] = InspectFormInput
    read_only: bool = True
    idempotent: bool = True

    async def _arun(self, **kwargs: Any) -> str:
        svc = self._service
        fp = svc.resolve_file_attachment(kwargs["file_path"], ".pdf")

        def _run():
            return inspect_form(file_path=fp)

        fields = await asyncio.to_thread(_run)
        return json.dumps(fields, ensure_ascii=False)


class FillFormTool(AgentTool):
    name: str = "fill_form"
    description: str = "Fill form fields in a PDF or DOCX. Returns a download URL."
    args_schema: type[BaseModel] = FillFormInput
    task_support: str | None = "optional"

    async def _arun(self, **kwargs: Any) -> str:
        svc = self._service
        data = kwargs["data"]
        field_data = json.loads(data) if isinstance(data, str) else data
        fp = svc.resolve_file_attachment(kwargs["file_path"], ".pdf")
        filename = kwargs.get("filename", "")
        if not filename:
            ext = fp.suffix or ".pdf"
            filename = f"filled{ext}"
        safe_name = f"{uuid.uuid4().hex[:8]}_{filename}"
        out_path = svc.output_dir / safe_name

        def _run():
            return fill_form(file_path=fp, output_path=out_path, data=field_data)

        result_path = await asyncio.to_thread(_run)
        return NamedFile(path=Path(result_path), name=filename)


class MergeSlidesTool(AgentTool):
    name: str = "merge_slides"
    description: str = (
        "Merge slides from multiple PPTX files into one. "
        "Pass each PPTX as file1..file5, use slides1..slides5 for indices."
    )
    args_schema: type[BaseModel] = MergeSlidesInput
    task_support: str | None = "optional"

    async def _arun(self, **kwargs: Any) -> str:
        svc = self._service
        from .composition._slide_merge import SlideRef

        files = [kwargs.get(f"file{i}", "") for i in range(1, 6)]
        slide_specs = [kwargs.get(f"slides{i}", "") for i in range(1, 6)]

        src_map: dict[str, str] = {}
        slide_refs: list[SlideRef] = []

        for raw_file, spec in zip(files, slide_specs, strict=True):
            if not raw_file:
                continue
            resolved = svc.resolve_file_attachment(raw_file, ".pptx")
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

        output_filename = kwargs.get("output_filename", "merged.pptx")
        safe = f"{uuid.uuid4().hex[:8]}_{output_filename}"
        out = svc.output_dir / safe

        def _run():
            return merge_slides(config, out, backend=kwargs.get("backend", "auto"))

        result = await asyncio.to_thread(_run)
        return NamedFile(path=result.output_path, name=output_filename)


class GetExamplesTool(AgentTool):
    name: str = "get_examples"
    description: str = (
        "Get reference Markdown examples for all composition tools. "
        "Returns examples for general documents, Marp slides, and pandoc slides."
    )
    args_schema: type[BaseModel] = GetExamplesInput
    read_only: bool = True
    idempotent: bool = True

    async def _arun(self, **kwargs: Any) -> str:
        # Delegate to the service's _get_examples which has the example constants
        return await self._service._get_examples()


def get_document_tools(service: Any) -> list[AgentTool]:
    """Create all document-agent tools bound to a service instance."""
    tools = [
        DigestDocumentTool(),
        ComposeDocumentTool(),
        GenerateDiagramTool(),
        GenerateImageTool(),
        InspectFormTool(),
        FillFormTool(),
        MergeSlidesTool(),
        GetExamplesTool(),
    ]
    for t in tools:
        t.bind_service(service)
    return tools
