#!/usr/bin/env bash
# Start the offline sample dashboard and its worker; Ctrl+C stops both.
set -euo pipefail

task_script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
task_project_dir="$(cd -- "${task_script_dir}/.." && pwd)"
cd "${task_project_dir}"
export UV_PROJECT_ENVIRONMENT="${task_project_dir}/.venv-demo"

uv sync --locked --no-dev
uv run --locked --no-dev evidence-explorer seed

task_worker_pid=""
cleanup() {
  if [[ -n "${task_worker_pid}" ]]; then
    kill "${task_worker_pid}" 2>/dev/null || true
    wait "${task_worker_pid}" 2>/dev/null || true
  fi
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

uv run --locked --no-dev evidence-explorer worker &
task_worker_pid=$!
uv run --locked --no-dev evidence-explorer web
