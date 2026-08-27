# ticketflow development recipes — run `just` to list them.

set shell := ["bash", "-euo", "pipefail", "-c"]

# List available recipes
default:
    @just --list

# Create the venv and install all dependencies (locked)
install:
    uv sync --locked --all-groups

# Auto-format and auto-fix lint findings
fmt:
    uv run ruff format .
    uv run ruff check --fix .

# Lint (format check + rules), no changes
lint:
    uv run ruff format --check .
    uv run ruff check .

# Static type check (mypy strict)
typecheck:
    uv run mypy

# Run the test suite (extra args pass through, e.g. `just test -k lease`)
test *ARGS:
    uv run pytest {{ ARGS }}

# Tests with coverage report (enforces the fail_under threshold)
cov:
    uv run pytest --cov --cov-report=term --cov-report=xml

# End-to-end tests against real external services (opt-in, needs credentials)
e2e *ARGS:
    uv run pytest -m e2e {{ ARGS }}

# Audit locked dependencies for known vulnerabilities
audit:
    uv export --locked --no-emit-project --format requirements-txt > requirements-audit.txt
    uvx pip-audit --strict -r requirements-audit.txt --disable-pip
    rm -f requirements-audit.txt

# Everything CI runs: lint, typecheck, tests with coverage
check: lint typecheck cov

# Run the ticketflow CLI (args pass through, e.g. `just run -- status`)
run *ARGS:
    uv run ticketflow {{ ARGS }}

# Remove caches and build artifacts (leaves the venv and run state alone)
clean:
    rm -rf .mypy_cache .ruff_cache .pytest_cache .coverage coverage.xml htmlcov dist build
