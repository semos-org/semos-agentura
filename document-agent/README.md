# document-agent

Document digestion (OCR to Markdown) and composition (Markdown to documents).

## Digestion

Convert PDF, images, and Office documents to Markdown using Mistral Document AI (direct or via Azure AI Foundry).

```bash
# File to markdown (writes .md + images to disk)
document-agent digest document.pdf

# Inline mode (returns markdown with base64-embedded images)
document-agent digest document.pdf --inline

# With structured annotation extraction
document-agent digest document.pdf --schema schema.py --prompt "Extract all line items"
```

Supported input formats: PDF, PNG, JPG, JPEG, WEBP, TIFF, BMP, DOCX, PPTX, XLSX, ODT.

## Composition

Convert Markdown to various output formats.

```bash
# Documents
document-agent compose input.md output.pdf --format pdf
document-agent compose input.md output.docx --format docx
document-agent compose input.md output.odt --format odt

# Slides (via Marp)
document-agent compose input.md output.html --format html --slides
document-agent compose input.md output.pdf --format pdf --slides
document-agent compose input.md output.pptx --format pptx --slides
```

Mermaid diagrams in fenced code blocks are automatically rendered as images.

## Setup

```bash
uv sync
cp .env.example .env
# Edit .env with your API keys
```

### Node.js tools (Marp, Mermaid)

Marp CLI and Mermaid CLI are installed locally via npm in the `tools/` subdirectory:

```bash
cd tools && npm install
```

The agent automatically discovers them in `tools/node_modules/.bin/`.

### Other external tools

- [Pandoc](https://pandoc.org/) - document conversion (PDF, DOCX, ODT) - install system-wide
- [LibreOffice](https://www.libreoffice.org/) - Office format input conversion - install system-wide

## Python API

```python
from document_agent import digest, compose, OutputFormat, OutputMode

# Digest a document
result = digest("document.pdf", output_mode=OutputMode.INLINE)
print(result.markdown)

# Compose a document
result = compose("input.md", "output.pdf", OutputFormat.PDF)
```
