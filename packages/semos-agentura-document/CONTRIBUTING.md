# Contributing to semos-agentura-document

Part of the [Semos Agentura](../../README.md) monorepo. Read the
[root CONTRIBUTING guide](../../CONTRIBUTING.md) for setup, the dev loop, and conventions.

- **Import:** `from semos.agentura.document import ...`
- **What it is:** the document agent: OCR digest, compose to PDF/PPTX/DOCX/HTML, diagram
  generation (Mermaid, draw.io), and PDF/DOCX form inspection and filling. Depends on
  `semos-agentura-core`.

```bash
make install                         # from the repo root, once
cd packages/semos-agentura-document
uv run pytest tests/                 # this package's tests
```

Tests needing external renderers (pandoc, mermaid-cli, marp, draw.io) skip when the tool is
absent, so they pass without a full toolchain. Cross-package end-to-end tests live in the
repo-root `tests/integration/`.
