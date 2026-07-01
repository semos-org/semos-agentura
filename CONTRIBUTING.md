# Contributing

Thanks for improving Semos Agentura. This is a [uv](https://docs.astral.sh/uv/) workspace
monorepo: one repository holding several independently installable `semos-agentura-*` packages
under `packages/`.

## Setup

Requirements: Python 3.11+ and uv.

```bash
make install   # uv sync --all-packages, then installs the pre-commit hooks
```

## Layout

```text
packages/
  semos-agentura-core/       # semos.agentura.core       shared MCP + A2A framework
  semos-agentura-email/      # semos.agentura.email      email + calendar agent
  semos-agentura-document/   # semos.agentura.document   OCR, compose, diagrams, forms
  semos-agentura-files/      # semos.agentura.files      virtual filesystem agent
  semos-agentura-ui/         # semos.agentura.ui         chat UI client
  semos-agentura/            # umbrella (installs the whole suite)
tests/integration/           # cross-package end-to-end tests
```

Every package ships into the shared `semos.agentura` PEP 420 namespace, laid out as
`src/semos/agentura/<leaf>/` with no `__init__.py` above the leaf. Each package installs on its
own; the agents depend on `semos-agentura-core`.

## Dev loop

```bash
make check              # lock check, pre-commit (ruff), ty, deptry. Run before every push.
make test               # per-package unit tests with coverage
make test-integration   # cross-package end-to-end tests (starts agents with mocked backends)
make docs               # serve the docs locally (make docs-test builds them strictly)
```

`make check` is the quality gate and must pass. Lint and format use ruff, typing uses `ty`,
dependency hygiene uses `deptry`, all wired through pre-commit. Declare what you import: each
package lists its own dependencies so it stays standalone.

## Making a change

1. Branch off `main`.
2. Work inside the relevant `packages/<name>/`, adding tests under its `tests/`.
3. Run `make check` and `make test` (add `make test-integration` when you touch cross-agent
   behaviour).
4. Open a focused PR to `main`. CI (quality, docs, tests, integration) must be green.

## Adding a new agent

Model it on an existing agent package: create `packages/semos-agentura-<name>/`, lay code out as
`src/semos/agentura/<name>/`, depend on `semos-agentura-core`, and subclass `BaseAgentService`.
The workspace already picks up `packages/*`, so no root wiring is needed. See
[Adding a New Agent](README.md#adding-a-new-agent).

## Conventions

- Keep the public import surface stable (`from semos.agentura.<leaf> import ...`).
- Small, reviewable commits that explain the why.
- Update `docs/` when behaviour changes.
