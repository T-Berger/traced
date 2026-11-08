#!/bin/bash
set -euo pipefail

# Railway entrypoint for Traced integrated workspace
# This script initializes the workspace and starts all dashboards

cd /app

# Ensure data directory exists
export EVIDENCE_DATA_DIR="${EVIDENCE_DATA_DIR:-/app/data}"
mkdir -p "$EVIDENCE_DATA_DIR"

# Set default port
export TRACED_PORT="${TRACED_PORT:-8000}"
export EVIDENCE_HOST="${EVIDENCE_HOST:-0.0.0.0}"

# Initialize the database if it doesn't exist
if [ ! -f "$EVIDENCE_DATA_DIR/evidence.sqlite3" ]; then
    echo "Initializing workspace database..."
    python -c "
import os
from pathlib import Path
from evidence_explorer.review.integrations.workspace import WorkspaceRepository, WorkspaceService
from evidence_explorer.config import Settings

settings = Settings(
    data_dir=Path(os.environ['EVIDENCE_DATA_DIR']),
    db_path=Path(os.environ['EVIDENCE_DATA_DIR']) / 'evidence.sqlite3',
    read_only=False
)
repo = WorkspaceRepository(settings.db_path)
repo.initialize()
print('Database initialized')
"
fi

# Start the integrated workspace with Gunicorn
exec gunicorn \
    --bind "$EVIDENCE_HOST:$TRACED_PORT" \
    --workers 2 \
    --threads 4 \
    --timeout 120 \
    --worker-class uvicorn.workers.UvicornWorker \
    --log-level info \
    evidence_explorer.review.integrations.web:create_app
