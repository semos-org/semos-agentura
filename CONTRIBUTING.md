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

## Releasing

Releases are fully automated with [python-semantic-release](https://python-semantic-release.readthedocs.io/):
every push to `main` runs the Release workflow, which versions each package independently from
[Conventional Commits](https://www.conventionalcommits.org/):

- `fix:` and `perf:` release a patch bump, `feat:` a minor bump, and any type marked with `!`
  or a `BREAKING CHANGE:` footer a major bump. Other types (`chore:`, `docs:`, `ci:`, ...)
  never release.
- Attribution is **path-based, not scope-based**: a commit bumps exactly the packages under
  `packages/<name>/` whose files it touches. A scope in brackets is optional and purely
  decorative; `feat: add CalDAV sync` and `feat(email): add CalDAV sync` release identically.
  Only the type keyword and the touched paths matter.
- Consequences: a commit touching files in two package dirs bumps both packages (so keep
  commits scoped to one package where possible), and a `feat:`/`fix:` touching only root-level
  files (docs, Makefile, workflows) releases nothing because no package path matches.
- Commit **bodies** are parsed too (squash-merge support): any body line starting with `fix:`,
  `feat:`, or another bump keyword counts as an embedded conventional commit. Never quote such
  keywords at the start of a line in a commit message; indent or rephrase them.
- Whenever any subpackage bumps, the umbrella `semos-agentura` bumps by the **same increment
  level** on its own version line; untouched subpackages keep their versions.
- Each release writes the package's `CHANGELOG.md`, tags (`semos-agentura-<name>-vX.Y.Z`,
  umbrella `vX.Y.Z`), creates a GitHub Release, and publishes the bumped packages to PyPI.

### Example: changing a single package

Starting state: every package is at 1.0.0. You commit

```text
feat(email): add CalDAV calendar sync
```

touching only files under `packages/semos-agentura-email/`. When the commit lands on `main`,
the Release workflow processes it like this:

1. `semos-agentura-core`, `-document`, `-files`, `-ui`: no commits touched their paths, so
   nothing happens. They stay at 1.0.0 with no new tags.
2. `semos-agentura-email`: the `feat` commit matches its path filter, so it gets a minor bump
   to 1.1.0. PSR writes `version = "1.1.0"` into its pyproject, refreshes `uv.lock`, prepends
   the 1.1.0 section to its `CHANGELOG.md`, commits, tags `semos-agentura-email-v1.1.0`, and
   creates the GitHub Release.
3. `semos-agentura` (umbrella): the same commit matches its cross-package path filters. The
   largest increment among the changes is minor, so the umbrella goes from 1.0.0 to 1.1.0
   with tag `v1.1.0`, its own changelog entry, and a GitHub Release.
4. Publish: only `semos-agentura-email` 1.1.0 and `semos-agentura` 1.1.0 are built and
   uploaded to PyPI. The other four packages are not rebuilt or republished.

The same flow with `fix(email): ...` instead would produce 1.0.1 for email and 1.0.1 for the
umbrella; with `feat(email)!: ...` (breaking) both would go to 2.0.0. Version lines drift apart
over time, and that is the point: a later `fix(files): ...` would release files 1.0.1 while the
umbrella, already at 1.1.0, moves to 1.1.1.

Preview what the next push to `main` would release with `make release-dry`.

## Adding a new agent

Model it on an existing agent package: create `packages/semos-agentura-<name>/`, lay code out as
`src/semos/agentura/<name>/`, depend on `semos-agentura-core`, and subclass `BaseAgentService`.
The workspace already picks up `packages/*`, so no root wiring is needed. See
[Adding a New Agent](README.md#adding-a-new-agent).

## Conventions

- Keep the public import surface stable (`from semos.agentura.<leaf> import ...`).
- Small, reviewable commits that explain the why.
- Update `docs/` when behaviour changes.
