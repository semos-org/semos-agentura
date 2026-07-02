# Contributing to semos-agentura-email

Part of the [Semos Agentura](../../README.md) monorepo. Read the
[root CONTRIBUTING guide](../../CONTRIBUTING.md) for setup, the dev loop, and conventions.

- **Import:** `from semos.agentura.email import ...`
- **What it is:** the email and calendar agent (IMAP/SMTP, Outlook COM, MS Graph) with the
  `@mailgent` LLM agent. Depends on `semos-agentura-core`.

```bash
make install                      # from the repo root, once
cd packages/semos-agentura-email
uv run pytest tests/              # this package's tests
```

COM tests need real Windows and Outlook, so they are marked `integration` and skipped in normal
runs. Cross-package end-to-end tests live in the repo-root `tests/integration/`.
