#!/usr/bin/env bash
# Run every automated check; stops at the first failure.
# Usage: ./scripts/check.sh   (set PYTHON to use a specific interpreter)
set -euo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")/.."
PYTHON="${PYTHON:-python}"

echo "==> ruff check"
"$PYTHON" -m ruff check .

echo "==> ruff format --check"
"$PYTHON" -m ruff format --check .

echo "==> mypy"
"$PYTHON" -m mypy

echo "==> pytest"
"$PYTHON" -m pytest tests/ -q

echo "All checks passed"
