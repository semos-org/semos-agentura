# Workspace packages (uv monorepo, under packages/)
PKGS := semos-agentura-core semos-agentura-email semos-agentura-document semos-agentura-files semos-agentura-ui
# Packages whose test suites run in CI (semos-agentura-ui excluded, matching current workflow)
TEST_PKGS := semos-agentura-core semos-agentura-email semos-agentura-document semos-agentura-files
# Source dirs for static analysis
SRC_DIRS := packages/semos-agentura-core/src packages/semos-agentura-email/src packages/semos-agentura-document/src packages/semos-agentura-files/src packages/semos-agentura-ui/src

.PHONY: install
install: ## Install the virtual environment and install the pre-commit hooks
	@echo "🚀 Creating virtual environment using uv"
	@uv sync --all-packages
	@uv run pre-commit install

.PHONY: check
check: ## Run code quality tools.
	@echo "🚀 Checking lock file consistency with 'pyproject.toml'"
	@uv lock --locked
	@echo "🚀 Linting code: Running pre-commit"
	@uv run pre-commit run -a
	@echo "🚀 Static type checking: Running ty"
	@uv run ty check $(SRC_DIRS)
	@echo "🚀 Checking for obsolete dependencies: Running deptry"
	@for pkg in $(PKGS); do \
		echo "  → deptry $$pkg"; \
		(cd packages/$$pkg && uv run deptry src) || exit 1; \
	done

.PHONY: test
test: ## Test the code with pytest (per package, with coverage)
	@echo "🚀 Testing code: Running pytest"
	@for pkg in $(TEST_PKGS); do \
		echo "=== $$pkg ==="; \
		(cd packages/$$pkg && uv run pytest tests/ --timeout=60 --tb=short -q \
			--cov=src/ --cov-report=xml:../../coverage-$$pkg.xml) || exit 1; \
	done

.PHONY: test-integration
test-integration: ## Run cross-package integration tests (starts agents; pandoc/drawio optional)
	@echo "🚀 Running cross-package integration tests"
	@uv run pytest tests/integration --timeout=120 --tb=short -q

.PHONY: build
build: clean-build ## Build wheels for all workspace packages
	@echo "🚀 Creating wheel files"
	@uv build --all-packages

.PHONY: clean-build
clean-build: ## Clean build artifacts
	@echo "🚀 Removing build artifacts"
	@uv run python -c "import shutil; import os; shutil.rmtree('dist') if os.path.exists('dist') else None"

.PHONY: publish
publish: ## Publish all workspace packages to PyPI (CI does this via trusted publishing; local use needs a token).
	@echo "🚀 Publishing."
	@uv publish

.PHONY: build-and-publish
build-and-publish: build publish ## Build and publish.

# Releases are automated: python-semantic-release runs on every push to main and bumps
# the packages whose paths the conventional commits touched (umbrella last, same
# increment). See .github/workflows/on-release-main.yml.
.PHONY: release-dry
release-dry: ## Preview the next version of every package from unreleased conventional commits
	@for pkg in $(PKGS) semos-agentura; do \
		next=$$(cd packages/$$pkg && uv run semantic-release version --print 2>/dev/null); \
		printf '%-25s %s\n' "$$pkg" "$${next:-no release (only main releases)}"; \
	done

.PHONY: docs-test
docs-test: ## Test if documentation can be built without warnings or errors
	@uv run zensical build -s

.PHONY: docs
docs: ## Build and serve the documentation
	@uv run zensical serve

.PHONY: help
help:
	@uv run python -c "import re; \
	[[print(f'\033[36m{m[0]:<20}\033[0m {m[1]}') for m in re.findall(r'^([a-zA-Z_-]+):.*?## (.*)$$', open(makefile).read(), re.M)] for makefile in ('$(MAKEFILE_LIST)').strip().split()]"

.DEFAULT_GOAL := help
