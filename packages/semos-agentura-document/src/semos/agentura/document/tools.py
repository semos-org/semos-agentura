"""AgentTool definitions for document-agent.

Each tool has a Pydantic input model for validation + schema generation,
and an AgentTool subclass with the async implementation.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import shutil
import uuid
from enum import Enum
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field, model_validator
from semos.agentura.core import AgentTool, FileAttachment, NamedFile, ToolResult

from . import (
    MergeConfig,
    compose,
    digest,
    fill_form,
    generate_diagram,
    generate_image_fn,
    inspect_form,
    inspect_form_visual,
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
    VISUAL = "visual"


class TrackChanges(str, Enum):
    ACCEPT = "accept"
    REJECT = "reject"
    ALL = "all"


class ComposeFormat(str, Enum):
    PDF = "pdf"
    PPTX = "pptx"
    DOCX = "docx"
    ODT = "odt"
    HTML = "html"


class TemplateBackend(str, Enum):
    AUTO = "auto"
    COM = "com"
    UNO = "uno"
    DOCKER = "docker"


class DiagramType(str, Enum):
    MERMAID = "mermaid"
    DRAWIO = "drawio"


class ImageMode(str, Enum):
    GENERATE = "generate"
    EDIT = "edit"
    CUT = "cut"


class ImageBackground(str, Enum):
    AUTO = "auto"
    TRANSPARENT = "transparent"
    WHITE = "white"


class ImageFormat(str, Enum):
    PNG = "png"
    WEBP = "webp"


class MergeBackend(str, Enum):
    AUTO = "auto"
    COM = "com"
    PPTX = "pptx"


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
        ge=1,
        le=1000,
        description="Maximum number of pages to OCR.",
    )
    digest_mode: DigestMode = Field(
        default=DigestMode.AUTO,
        description=(
            "'auto' (pandoc for DOCX/ODT, OCR otherwise), 'ocr', 'pandoc', or "
            "'visual' (render pages and run a VLM to read filled form content "
            "and validate field placement)."
        ),
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
    source_file: FileAttachment | str = Field(
        default="",
        description="Markdown file to compose (file path or upload). Provide this OR source_markdown.",
        json_schema_extra={"x-file": True},
    )
    source_markdown: str = Field(
        default="",
        description="Raw Markdown text to compose. Provide this OR source_file.",
    )

    @model_validator(mode="after")
    def _exactly_one_source(self) -> ComposeInput:
        has_file = bool(self.source_file)
        has_md = bool(self.source_markdown)
        if not has_file and not has_md:
            raise ValueError("Provide either source_file or source_markdown")
        if has_file and has_md:
            raise ValueError("Provide source_file or source_markdown, not both")
        return self

    format: ComposeFormat = Field(
        description="Output format: 'pdf', 'pptx', 'docx', 'odt', or 'html'.",
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
    template_backend: TemplateBackend = Field(
        default=TemplateBackend.AUTO,
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
    diagram_type: DiagramType = Field(
        default=DiagramType.MERMAID,
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
    mode: ImageMode = Field(
        default=ImageMode.GENERATE,
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
        pattern=r"^\d+x\d+$",
        description="Output size as WIDTHxHEIGHT (e.g. '1024x1024', '512x512').",
    )
    background: ImageBackground = Field(
        default=ImageBackground.AUTO,
        description="'auto', 'transparent', or 'white'.",
    )
    output_format: ImageFormat = Field(
        default=ImageFormat.PNG,
        description="'png' or 'webp'.",
    )


class InspectFormInput(BaseModel):
    file_path: FileAttachment | str = Field(
        description="PDF or DOCX to inspect (file path, base64, or data URI).",
        json_schema_extra={"x-file": True},
    )
    visual: bool = Field(
        default=False,
        description=(
            "Enable when form keys are cryptic / not self-explaining. Fills the "
            "form with probe values, renders pages, and uses a VLM to learn each "
            "field's human label. Slower; needs LibreOffice for DOCX."
        ),
    )
    max_iterations: int = Field(
        default=3,
        ge=1,
        le=10,
        description="Visual mode: max probe/render/review cycles.",
    )


class FillFormInput(BaseModel):
    file_path: FileAttachment | str = Field(
        description="PDF or DOCX to fill (file path, base64, or data URI).",
        json_schema_extra={"x-file": True},
    )
    data: str | dict = Field(
        description=(
            "Field values to write. Keys MUST be the field's x-field-id from "
            "inspect_form's schema (the real fill key), NOT the human label. "
            "Values by type: text/date -> str, checkbox -> bool, radio/dropdown "
            "-> the option value. Accepts a dict or a JSON string. You may also "
            "pass the whole {schema, data} object from inspect_form."
        ),
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
    backend: MergeBackend = Field(
        default=MergeBackend.AUTO,
        description="'auto', 'com', or 'pptx'.",
    )


class GetExamplesInput(BaseModel):
    pass


# Tool implementations


def _form_content_hash(path: Path) -> str:
    """SHA1 of file bytes - stable across the temp paths of repeated uploads."""
    return hashlib.sha1(Path(path).read_bytes()).hexdigest()


def _cache_form_resolver(svc: Any, fp: Path, schema: dict) -> None:
    """Cache a label/title -> x-field-id resolver for a later fill_form call."""
    cache = getattr(svc, "_form_resolver_cache", None)
    if cache is None:
        cache = {}
        svc._form_resolver_cache = cache
    field_ids: set[str] = set()
    label_to_id: dict[str, str] = {}
    for prop in schema.get("properties", {}).values():
        fid = prop.get("x-field-id")
        if not fid:
            continue
        field_ids.add(fid)
        title = prop.get("title")
        if title and title != fid:
            label_to_id.setdefault(title, fid)
    cache[_form_content_hash(fp)] = {"field_ids": field_ids, "label_to_id": label_to_id}


def _resolve_fill_keys(svc: Any, fp: Path, data: Any) -> Any:
    """Remap label-keyed fill data to x-field-id using the cached resolver.

    Lets fill_form succeed even when the model keys by the human label/title
    instead of the real field id. Keys already matching a field id, and the
    {schema, data} form, pass through untouched.
    """
    if not isinstance(data, dict) or "schema" in data:
        return data
    entry = (
        getattr(svc, "_form_resolver_cache", {}).get(_form_content_hash(fp))
        if hasattr(svc, "_form_resolver_cache")
        else None
    )
    if not entry:
        return data
    field_ids = entry["field_ids"]
    label_to_id = entry["label_to_id"]
    lower = {lbl.lower(): fid for lbl, fid in label_to_id.items()}
    out: dict[str, Any] = {}
    for key, value in data.items():
        if key in field_ids:
            out[key] = value
        elif key in label_to_id:
            out[label_to_id[key]] = value
        elif str(key).lower() in lower:
            out[lower[str(key).lower()]] = value
        else:
            out[key] = value
    return out


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

        if kwargs.get("source_file"):
            source_path = svc.resolve_file_attachment(kwargs["source_file"], ".md")
        else:
            tmp_md = svc.output_dir / f"_source_{filename}.md"
            tmp_md.write_text(kwargs["source_markdown"], encoding="utf-8")
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
                template_backend=TemplateBackend(kwargs.get("template_backend", TemplateBackend.AUTO)).value,
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
            diagram_type=DiagramType(kwargs.get("diagram_type", DiagramType.MERMAID)).value,
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
            mode=ImageMode(kwargs.get("mode", ImageMode.GENERATE)).value,
            source=src_path,
            mask=mask_path,
            style=kwargs.get("style", ""),
            size=kwargs.get("size", "1024x1024"),
            background=ImageBackground(kwargs.get("background", ImageBackground.AUTO)).value,
            output_format=ImageFormat(kwargs.get("output_format", ImageFormat.PNG)).value,
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
    description: str = (
        "Inspect form fields in a PDF or DOCX. Returns a JSON Schema of the "
        "fields (each leaf carries x-field-id = the real fill key) plus a data "
        "dict of current values. Set visual=true when keys are cryptic to learn "
        "human labels from the rendered pages."
    )
    args_schema: type[BaseModel] = InspectFormInput
    read_only: bool = True
    idempotent: bool = True

    async def _arun(self, **kwargs: Any) -> str:
        svc = self._service
        fp = svc.resolve_file_attachment(kwargs["file_path"], ".pdf")

        if kwargs.get("visual"):
            result = await inspect_form_visual(
                fp,
                max_iterations=kwargs.get("max_iterations", 3),
                output_dir=svc.output_dir,
                settings=svc._settings,
            )
            validation = result.pop("validation_path", None)
            _cache_form_resolver(svc, fp, result.get("schema", {}))
            # Return the schema as structured data so it survives in
            # structuredContent alongside the validation file (a file-only
            # ToolResult would push the schema out of structuredContent).
            if validation:
                vpath = Path(validation)
                return ToolResult(
                    data=result,
                    files=[NamedFile(path=vpath, name=f"{fp.stem}_validation{vpath.suffix}")],
                )
            return ToolResult(data=result)

        result = await asyncio.to_thread(inspect_form, fp)
        _cache_form_resolver(svc, fp, result.get("schema", {}))
        return ToolResult(data=result)


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
        # Map label/title-keyed data back to x-field-id via the resolver cached
        # by a preceding inspect_form call (robust to the model mis-keying).
        field_data = _resolve_fill_keys(svc, fp, field_data)
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
            return merge_slides(config, out, backend=MergeBackend(kwargs.get("backend", MergeBackend.AUTO)).value)

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
