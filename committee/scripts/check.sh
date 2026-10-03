#!/usr/bin/env bash
# The local equivalent of CI. Run from committee/.
set -euo pipefail
uv run ruff check src tests
uv run ruff format --check src tests
uv run mypy
uv run pytest -q -p no:warnings "$@"
