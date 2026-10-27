#!/usr/bin/env bash
set -euo pipefail
repo_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$repo_dir"
venv_dir="${GEOFIND_VENV:-$repo_dir/.venv}"
export PIP_CACHE_DIR="$repo_dir/.cache/pip"
python3 -m venv "$venv_dir"
"$venv_dir/bin/python" -m pip install --disable-pip-version-check -r requirements.lock
"$venv_dir/bin/python" -m pip check
