#!/usr/bin/env bash
set -euo pipefail
repo_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$repo_dir"
venv_dir="${GEOFIND_VENV:-$repo_dir/.venv}"
exec "$venv_dir/bin/python" -m streamlit run app.py --server.address 0.0.0.0 --server.port "${PORT:-8501}"
