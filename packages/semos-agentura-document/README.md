# semos-agentura-document

Document digestion (OCR to Markdown), composition (Markdown to documents), diagram generation, raster image generation, and form filling. Full round-trip support for footnotes, tracked changes, comments, and styles.

## Digestion

Convert PDF, images, and Office documents to Markdown.

- **PDF/images**: OCR via Mistral Document AI (direct or Azure AI Foundry)
- **DOCX/ODT**: pandoc-based extraction preserving footnotes, tracked changes, comments, and document styles

```bash
# Basic digest (auto-selects pandoc for DOCX, OCR for PDF/images)
semos-agentura-document digest document.docx

# Show all tracked changes and comments
semos-agentura-document digest document.docx --track-changes all

# Force OCR pipeline for a scanned DOCX
semos-agentura-document digest scanned.docx --mode ocr

# Inline mode (base64-embedded images, prints to stdout)
semos-agentura-document digest document.pdf --inline

# With structured annotation extraction
semos-agentura-document digest document.pdf --schema schema.py --prompt "Extract all line items"
```

### Round-trip metadata

DOCX digestion preserves four types of metadata that round-trip through compose:

- **Footnotes**: `[^1]` with `[^1]: definition text` at end
- **Tracked changes**: `[text]{.insertion author="Name" date="2026-01-01"}` / `{.deletion ...}`
- **Comments**: `[comment text]{.comment-start id="1" author="Name"}...{.comment-end id="1"}`
- **Document styles**: YAML front matter block (fonts, sizes, colors, spacing, margins)

Supported input formats: PDF, PNG, JPG, JPEG, WEBP, TIFF, BMP, DOCX, PPTX, XLSX, ODT.

## Composition

Convert Markdown to various output formats with style control.

```bash
# Basic compose
semos-agentura-document compose input.md output.docx --format docx

# With a reference document for style inheritance
semos-agentura-document compose input.md output.docx --format docx \
  --reference-doc template.docx

# With headers/footers from a template (combined with YAML styles)
semos-agentura-document compose input.md output.docx --format docx \
  --header-footer-doc template.docx

# Slides - polished (Marp, limited editability)
semos-agentura-document compose slides.md out.pptx --format pptx --slides

# Slides - draft editable (pandoc, fully editable, rough layout)
semos-agentura-document compose slides.md out.pptx --format pptx --slides --draft

# Slides - draft + corporate template (requires PowerPoint)
semos-agentura-document compose slides.md out.pptx --format pptx --slides --draft \
  --template corporate.pptx
```

### YAML front matter styles

Define document formatting directly in the Markdown source. Styles are auto-extracted during digest and auto-applied during compose:

```yaml
---
title: "Document Title"
subtitle: "Optional *formatted* subtitle"
styles:
  page:
    size: "A4"                    # or "Letter"
    margin-top: "1.5cm"
    margin-bottom: "1.5cm"
    margin-left: "1.5cm"
    margin-right: "1.5cm"
  body:
    font: "Calibri"
    size: 11
    spacing-before: "0.0cm"
    spacing-after: "0.1cm"
    line-spacing: 1.1
  heading1:
    font: "Calibri"
    size: 13
    bold: true
    color: "000080"               # navy
    spacing-before: "0.3cm"
    spacing-after: "0.1cm"
  heading2:
    font: "Calibri"
    size: 11
    bold: true
    color: "000080"
  heading3:
    font: "Calibri"
    size: 11
    bold: true
    color: "333333"
  table:
    size: 9                       # also used for footnotes and captions
    border-color: "999999"
    border-size: 4                # eighths of a point
    fixed: false                  # true to keep equal-width columns
---
```

Style priority: YAML front matter > `--reference-doc` > pandoc defaults.

Mermaid and draw.io diagrams in fenced code blocks are automatically rendered as images.

## Diagrams

Generate and modify diagrams (Mermaid or draw.io) using LLM-powered optimization.

```bash
# Generate from description
semos-agentura-document diagram "flowchart of CI/CD pipeline" -o diagram.png

# Modify existing draw.io diagram
semos-agentura-document diagram "Change WP3 label to Digital Infrastructure" \
  --source diagram.drawio.png -o updated.drawio.png \
  --code-output updated.drawio

# Mermaid diagram
semos-agentura-document diagram "sequence diagram for auth flow" --type mermaid -o auth.png
```

draw.io diagrams with embedded raster images are handled automatically:
images are stripped for LLM editing (reducing context from ~400 KB to ~8 KB)
and restored in the output. For correct PNG rendering of embedded images,
set `DRAWIO_DESKTOP_PATH` in `.env` (the npm CLI can't render inline images).

## Image Generation

Generate, edit, and extract elements from raster images using text-to-image models.

### Modes

- **generate**: Text-to-image from a prompt, with optional style prefix
- **edit**: Modify an existing image with a text prompt (inpainting with optional mask for OpenAI, img2img for Flux)
- **cut**: Extract a specific element from an image (VLM-guided bounding box + background removal, falls back to image model isolation)

### Supported providers

| Provider | Endpoint format | Generate | Edit | Models |
|----------|----------------|----------|------|--------|
| Azure OpenAI | `https://{resource}.cognitiveservices.azure.com/openai/deployments/{model}` | Yes | Yes (mask) | gpt-image-2 |
| OpenAI | `https://api.openai.com` | Yes | Yes (mask) | gpt-image-2, dall-e-3 |
| Azure AI Foundry | `https://{resource}.services.ai.azure.com/providers/{provider}` | Yes | Yes (img2img) | flux-2-pro, flux-2-dev |
| Direct provider | `https://api.bfl.ai` | Yes | Yes (img2img) | flux-2-pro, flux-2-dev |

Configure via environment variables:

```bash
IMAGE_GEN_ENDPOINT=https://example.cognitiveservices.azure.com/openai/deployments/gpt-image-2
IMAGE_GEN_API_KEY=your-key
IMAGE_GEN_MODEL=gpt-image-2
```

### Crossover: embedding images in diagrams

Generated images (icons, symbols) can be embedded into draw.io diagrams via the `embeds` parameter on `generate_diagram`. Refer to each embed by its filename in the diagram description so the LLM knows where to place them.

The embed mechanism reuses the existing draw.io image strip/restore pipeline: images are represented as `__IMG_N__` placeholders during LLM codegen and replaced with base64 data URIs before rendering.

## Forms

Inspect and fill form fields in PDF and DOCX files.

```bash
# Inspect form fields
semos-agentura-document inspect form.pdf
semos-agentura-document inspect form.docx --json

# Fill form fields
semos-agentura-document fill form.pdf filled.pdf --data '{"name": "John", "date": "2026-01-01"}'
semos-agentura-document fill form.docx filled.docx --data fields.json
```

## Slide Merge

Cherry-pick and merge slides from multiple PPTX sources.

```bash
# Merge specific slides by index
semos-agentura-document merge-slides deck1.pptx:0-5 deck2.pptx:3,7,12 -o merged.pptx

# Use a different base template for theme/master
semos-agentura-document merge-slides deck1.pptx:0-5 deck2.pptx:3,7 \
  --base template.pptx -o merged.pptx

# Force python-pptx backend (portable, no PowerPoint needed)
semos-agentura-document merge-slides deck.pptx:0-10 -o subset.pptx --backend pptx
```

Backends: COM (PowerPoint, preserves animations/transitions/media) is preferred.
Falls back to python-pptx (portable, pure Python) when PowerPoint is unavailable.
Install optional dependencies: `pip install semos-agentura-document[slides]`

## Setup

```bash
uv sync
cp .env.example .env
# Edit .env with your API keys and tool paths
```

### Node.js tools (Marp, Mermaid, draw.io CLI)

Marp CLI, Mermaid CLI, and draw.io export are installed locally via npm:

```bash
cd tools && npm install
```

The agent automatically discovers them in `tools/node_modules/.bin/`.

### Other external tools

- [Pandoc](https://pandoc.org/) - DOCX digest and document composition - install system-wide
- [LibreOffice](https://www.libreoffice.org/) - Office format OCR fallback - install system-wide
- [draw.io desktop](https://github.com/jgraph/drawio-desktop) - needed for rendering diagrams with embedded images. Set `DRAWIO_DESKTOP_PATH` in `.env`.

## Python API

```python
from semos.agentura.document import (
    digest, compose, compose_editable_slides,
    merge_slides, parse_source_args,
    OutputFormat, OutputMode,
)

# Digest a DOCX with tracked changes
result = digest("document.docx", track_changes="all")
print(result.markdown)

# Compose with YAML styles
result = compose("styled.md", "output.docx", OutputFormat.PDF)

# Compose with reference doc
result = compose("input.md", "output.docx", OutputFormat.DOCX,
                 reference_doc="template.docx")

# Slides - polished (Marp)
result = compose("slides.md", "out.pptx", OutputFormat.PPTX,
                 is_slides=True)

# Slides - draft editable (pandoc)
result = compose("slides.md", "out.pptx", OutputFormat.PPTX,
                 is_slides=True, draft=True)

# Slides - draft + corporate template
result = compose("slides.md", "out.pptx", OutputFormat.PPTX,
                 is_slides=True, draft=True,
                 template="corporate.pptx")

# Merge slides from CLI-style args
config = parse_source_args(
    ["deck1.pptx:0-5", "deck2.pptx:3,7"],
    output="merged.pptx",
)
result = merge_slides(config, "merged.pptx")
```
