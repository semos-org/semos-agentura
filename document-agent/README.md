# document-agent

Document digestion (OCR to Markdown) and composition (Markdown to documents) with full round-trip support for footnotes, tracked changes, comments, and styles.

## Digestion

Convert PDF, images, and Office documents to Markdown.

- **PDF/images**: OCR via Mistral Document AI (direct or Azure AI Foundry)
- **DOCX/ODT**: pandoc-based extraction preserving footnotes, tracked changes, comments, and document styles

```bash
# Basic digest (auto-selects pandoc for DOCX, OCR for PDF/images)
document-agent digest document.docx

# Show all tracked changes and comments
document-agent digest document.docx --track-changes all

# Force OCR pipeline for a scanned DOCX
document-agent digest scanned.docx --mode ocr

# Inline mode (base64-embedded images, prints to stdout)
document-agent digest document.pdf --inline

# With structured annotation extraction
document-agent digest document.pdf --schema schema.py --prompt "Extract all line items"
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
document-agent compose input.md output.docx --format docx

# With a reference document for style inheritance
document-agent compose input.md output.docx --format docx \
  --reference-doc template.docx

# With headers/footers from a template (combined with YAML styles)
document-agent compose input.md output.docx --format docx \
  --header-footer-doc template.docx

# Slides (via Marp)
document-agent compose input.md output.pptx --format pptx --slides
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
document-agent diagram "flowchart of CI/CD pipeline" -o diagram.png

# Modify existing draw.io diagram
document-agent diagram "Change WP3 label to Digital Infrastructure" \
  --source diagram.drawio.png -o updated.drawio.png \
  --code-output updated.drawio

# Mermaid diagram
document-agent diagram "sequence diagram for auth flow" --type mermaid -o auth.png
```

draw.io diagrams with embedded raster images are handled automatically:
images are stripped for LLM editing (reducing context from ~400 KB to ~8 KB)
and restored in the output. For correct PNG rendering of embedded images,
set `DRAWIO_DESKTOP_PATH` in `.env` (the npm CLI can't render inline images).

## Forms

Inspect and fill form fields in PDF and DOCX files.

```bash
# Inspect form fields
document-agent inspect form.pdf
document-agent inspect form.docx --json

# Fill form fields
document-agent fill form.pdf filled.pdf --data '{"name": "John", "date": "2026-01-01"}'
document-agent fill form.docx filled.docx --data fields.json
```

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
from document_agent import digest, compose, OutputFormat, OutputMode

# Digest a DOCX with tracked changes
result = digest("document.docx", track_changes="all")
print(result.markdown)  # includes footnotes, comments, styles

# Compose with YAML styles (auto-read from front matter)
result = compose("styled.md", "output.docx", OutputFormat.PDF)

# Compose with reference doc
result = compose("input.md", "output.docx", OutputFormat.DOCX,
                 reference_doc="template.docx")
```
