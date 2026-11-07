#!/usr/bin/env bash
set -euo pipefail

trail_project_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
cd -- "$trail_project_dir"
export UV_PROJECT_ENVIRONMENT="$trail_project_dir/.venv-trail"
exec uv run --locked --extra swarm python -m evidence_explorer.review.integrations \
  web --with-worker "$@"
