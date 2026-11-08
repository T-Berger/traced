"""WSGI entry point for the integrated Traced workspace with all dashboards.

This module provides a production-ready WSGI application that includes:
- Evidence Explorer dashboard
- Swarm Detector dashboard  
- Operation Silent Storm dashboard
- Integrated review workspace
"""

from __future__ import annotations

import os
from pathlib import Path

from evidence_explorer.config import Settings
from evidence_explorer.review.integrations.workspace import WorkspaceRepository, WorkspaceService
from evidence_explorer.review.integrations.web import create_app


def create_integrated_app():
    """Create and return the integrated Traced workspace application."""
    # Determine data directory
    data_dir = Path(os.getenv("EVIDENCE_DATA_DIR", "/app/data"))
    data_dir.mkdir(parents=True, exist_ok=True)
    
    # Create settings from environment
    settings = Settings.from_env()
    
    # Override data_dir if specified
    settings = Settings(
        data_dir=data_dir,
        ollama_url=settings.ollama_url,
        embedding_model=settings.embedding_model,
        chat_model=settings.chat_model,
        host=os.getenv("EVIDENCE_HOST", "0.0.0.0"),
        port=int(os.getenv("TRACED_PORT", os.getenv("PORT", "8000"))),
        read_only=os.getenv("EVIDENCE_READ_ONLY", "false").lower() in {"true", "1", "yes"},
    )
    
    # Initialize repository
    repository = WorkspaceRepository(settings.db_path)
    repository.initialize()
    
    # Create service
    service = WorkspaceService(
        repository,
        settings,
        settings.data_dir / "trail-artifacts",
    )
    
    # Create and return the app
    app = create_app(service)
    return app


# Create the application instance
app = create_integrated_app()
server = app.server

# Health check endpoint
@server.get("/health")
def health():
    return {"status": "ok", "service": "traced-integrated"}, 200


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Integrated Traced Workspace")
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=8000)
    args = parser.parse_args()
    app.run(host=args.host, port=args.port, debug=False)
