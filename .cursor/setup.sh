#!/usr/bin/env bash
set -euo pipefail

# Cursor cloud-agent bootstrap for gateway-focused work.
# Must be idempotent: install may run multiple times on cached machines.

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

if ! command -v python3 >/dev/null 2>&1; then
  echo "python3 is required but not found" >&2
  exit 1
fi

python3 -m pip install --upgrade pip setuptools wheel

# Install Hermes in editable mode with:
# - base runtime deps (from pyproject)
# - messaging deps (gateway adapters/tests)
# - dev deps (pytest, pytest-asyncio, pytest-xdist, etc.)
python3 -m pip install -e ".[messaging,dev]"
