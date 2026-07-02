# Contributing to semos-agentura-files

Part of the [Semos Agentura](../../README.md) monorepo. Read the
[root CONTRIBUTING guide](../../CONTRIBUTING.md) for setup, the dev loop, and conventions.

- **Import:** `from semos.agentura.files import ...`
- **What it is:** the virtual filesystem agent, a unified API over local, WebDAV/SharePoint, and
  archive backends via fsspec. Depends on `semos-agentura-core`.

```bash
make install                      # from the repo root, once
cd packages/semos-agentura-files
uv run pytest tests/              # this package's tests
```

Playwright browser tests and real cloud-credential tests are marked and skipped in normal runs.
Cross-package end-to-end tests live in the repo-root `tests/integration/`.
