#!/usr/bin/env bash
set -euo pipefail

skill_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
python_bin="${PYTHON_BIN:-python3}"
venv_dir="${FOOTBALL_MARKET_VENV:-$skill_root/.venv}"

"$python_bin" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else "需要 Python 3.10+；可设置 PYTHON_BIN=python3.12")'
"$python_bin" -m venv "$venv_dir"
"$venv_dir/bin/python" -m pip install --upgrade pip
"$venv_dir/bin/python" -m pip install -r "$skill_root/requirements.txt"


printf 'Runtime ready: %s\n' "$venv_dir/bin/python"
printf 'Run: %s %s\n' "$venv_dir/bin/python" "$skill_root/scripts/analyze_today.py"
