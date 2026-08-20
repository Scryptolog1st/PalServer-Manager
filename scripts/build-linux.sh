#!/usr/bin/env bash
set -euo pipefail
ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"
python3 -m pip install --upgrade build
python3 -m build
printf 'Packages written to %s/dist\n' "$ROOT_DIR"
