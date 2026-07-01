# Contributing to semos-agentura (umbrella)

Part of the [Semos Agentura](../../README.md) monorepo. Read the
[root CONTRIBUTING guide](../../CONTRIBUTING.md) for setup, the dev loop, and conventions.

This package ships **no code**. It is a meta-package whose only job is to depend on the five
`semos-agentura-*` packages so that `pip install semos-agentura` (or `uv add semos-agentura`)
installs the whole suite.

To change what the suite includes, edit the `dependencies` in this package's `pyproject.toml`.
There is nothing to test here.
