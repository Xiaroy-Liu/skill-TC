#!/usr/bin/env bash
set -euo pipefail

skill_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
repo_root="$(cd "$skill_root/../.." && pwd)"
python_bin="${PYTHON_BIN:-python3}"
venv_dir="${FOOTBALL_MARKET_VENV:-$repo_root/.venv}"

"$python_bin" -m venv "$venv_dir"
"$venv_dir/bin/python" -m pip install --upgrade pip
"$venv_dir/bin/python" -m pip install -r "$repo_root/requirements.txt"
"$venv_dir/bin/python" -m playwright install chromium

printf 'Runtime ready: %s\n' "$venv_dir/bin/python"
printf 'Set FOOTBALL_EIGHTBO_PYTHON=%s when running Scrapling collectors.\n' "$venv_dir/bin/python"
