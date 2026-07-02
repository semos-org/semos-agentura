# Contributing to semos-agentura-core

Part of the [Semos Agentura](../../README.md) monorepo. Read the
[root CONTRIBUTING guide](../../CONTRIBUTING.md) for setup, the dev loop, and conventions.

- **Import:** `from semos.agentura.core import ...`
- **What it is:** the shared MCP + A2A framework every agent builds on (`BaseAgentService`,
  `LLMExecutor`, `AgentTool`, `AgenturaClient`, `MCPHub`, `create_app`).

```bash
make install                     # from the repo root, once
cd packages/semos-agentura-core
uv run pytest tests/             # this package's tests
```

Keep this package free of imports from sibling agents. Cross-package end-to-end tests live in the
repo-root `tests/integration/` (`make test-integration`).
