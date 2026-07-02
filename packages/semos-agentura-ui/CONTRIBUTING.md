# Contributing to semos-agentura-ui

Part of the [Semos Agentura](../../README.md) monorepo. Read the
[root CONTRIBUTING guide](../../CONTRIBUTING.md) for setup, the dev loop, and conventions.

- **Import:** `from semos.agentura.ui import ...` (run it with `python -m semos.agentura.ui`)
- **What it is:** the Panel-based chat UI client. Connects to the agents over MCP and A2A and
  hosts the filesystem agent in-process. Depends on `semos-agentura-core` and
  `semos-agentura-files`.

```bash
make install                   # from the repo root, once
cd packages/semos-agentura-ui
uv run pytest tests/           # this package's tests
```

Agent connection URLs come from `SEMOS_AGENTURA_<AGENT>_URL` env vars (see `.env.example`).
Cross-package end-to-end tests live in the repo-root `tests/integration/`.
