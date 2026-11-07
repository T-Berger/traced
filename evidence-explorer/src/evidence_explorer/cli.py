"""Small command-line entry points for local development and snapshot hosting."""

import json
import logging
from dataclasses import replace
from pathlib import Path
from typing import Annotated

import typer

from .config import Settings
from .feeds.cli import register_feed_commands
from .ingestion_cli import register_ingestion_commands

app = typer.Typer(no_args_is_help=True, help="Explore news evidence locally.")
register_ingestion_commands(app)
register_feed_commands(app)


def _service():
    from .service import ExplorerService

    return ExplorerService(Settings.from_env())


def _json(value: object) -> None:
    typer.echo(json.dumps(value, indent=2, ensure_ascii=False, default=str))


@app.command()
def seed() -> None:
    """Load the clearly labeled fictional example corpus (safe to repeat)."""
    from .db import Repository
    from .demo import seed_demo

    settings = Settings.from_env()
    repo = Repository(settings.db_path, read_only=settings.read_only)
    repo.initialize()
    count = seed_demo(repo)
    typer.echo(f"Added {count} sample articles. Database: {settings.db_path}")


@app.command()
def web(
    port: Annotated[int | None, typer.Option(min=1, max=65535)] = None,
    host: Annotated[str | None, typer.Option(help="Default: EVIDENCE_HOST or 127.0.0.1")] = None,
) -> None:
    """Start the local dashboard; run the worker in a second terminal."""
    from .web import create_app

    settings = Settings.from_env()
    overrides = {}
    if port is not None:
        overrides["port"] = port
    if host is not None:
        overrides["host"] = host
    settings = replace(settings, **overrides)
    dashboard = create_app(settings)
    dashboard.run(host=settings.host, port=settings.port, debug=False)


@app.command()
def worker(
    once: Annotated[bool, typer.Option(help="Process queued jobs, then exit.")] = False,
) -> None:
    """Run one durable analysis worker for the current data directory."""
    from .worker import WorkerAlreadyRunning, run_worker

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    try:
        processed = run_worker(Settings.from_env(), once=once)
    except WorkerAlreadyRunning as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(1) from exc
    except KeyboardInterrupt:
        typer.echo("Worker stopped.")
        return
    typer.echo(f"Processed {processed} job(s).")


@app.command()
def analyze(
    mode: Annotated[str, typer.Option(help="demo or ollama")] = "demo",
    article_id: Annotated[
        int | None, typer.Option(min=1, help="Analyze only this article ID.")
    ] = None,
) -> None:
    """Queue corpus analysis; the worker performs inference."""
    if mode not in {"demo", "ollama"}:
        raise typer.BadParameter("Choose demo or ollama.", param_hint="--mode")
    service = _service()
    if article_id is not None and service.repo.get_article(article_id) is None:
        raise typer.BadParameter("Article ID does not exist.", param_hint="--article-id")
    job_id = service.enqueue_analysis(
        mode, article_ids=[article_id] if article_id is not None else None
    )
    typer.echo(f"Queued analysis job {job_id}. Run 'evidence-explorer worker --once'.")


@app.command()
def search(
    query: Annotated[str, typer.Argument(help="Words or a question to investigate.")],
    mode: Annotated[str, typer.Option(help="keyword, demo, or ollama")] = "keyword",
    limit: Annotated[int, typer.Option(min=1, max=100)] = 10,
) -> None:
    """Search immediately by keyword, or queue local semantic search."""
    if not query.strip():
        raise typer.BadParameter("Enter a nonempty search query.", param_hint="QUERY")
    if mode not in {"keyword", "demo", "ollama"}:
        raise typer.BadParameter("Choose keyword, demo, or ollama.", param_hint="--mode")
    service = _service()
    if mode == "keyword":
        _json(service.search_keyword(query, limit=limit))
    else:
        job_id = service.enqueue_search(query, mode, limit=limit)
        typer.echo(f"Queued search job {job_id}. Run 'evidence-explorer worker --once'.")


@app.command()
def doctor() -> None:
    """Inspect database, worker heartbeat, and local model-server availability."""
    _json(_service().status())


@app.command()
def export(
    destination: Annotated[
        Path, typer.Argument(help="Directory for a read-only database snapshot.")
    ],
) -> None:
    """Export a consistent SQLite snapshot, including its full-text index."""
    from .db import Repository

    settings = Settings.from_env()
    repo = Repository(settings.db_path, read_only=settings.read_only)
    repo.initialize()
    destination = destination.expanduser().resolve()
    if destination.exists() and not destination.is_dir():
        raise typer.BadParameter("Choose a directory path.", param_hint="DESTINATION")
    destination.mkdir(parents=True, exist_ok=True)
    output = repo.backup(destination / "evidence.sqlite3")
    typer.echo(f"Exported {output}")
    typer.echo("Serve with EVIDENCE_DATA_DIR set to this directory and EVIDENCE_READ_ONLY=true.")


if __name__ == "__main__":
    app()
