"""Small CLI adapters for queued news collection; network work stays in the worker."""

from typing import Annotated

import typer


def _enqueue(url: str, source_type: str, limit: int, topic: str, analyze: bool) -> None:
    from .service import ExplorerService

    try:
        job_id = ExplorerService().enqueue_ingestion(
            url, source_type=source_type, limit=limit, topic=topic, analyze=analyze
        )
    except (ValueError, PermissionError) as exc:
        typer.echo(f"Cannot queue import: {exc}", err=True)
        raise typer.Exit(2) from exc
    typer.echo(
        f"Queued {source_type} import job {job_id}. Keep the worker running to collect news."
    )
    if analyze:
        typer.echo(
            "Imported articles will then be analyzed with the configured local Ollama models."
        )


def register_ingestion_commands(app: typer.Typer) -> None:
    @app.command("ingest-url")
    def ingest_url(
        url: Annotated[str, typer.Argument(help="Public HTTP(S) article URL.")],
        topic: Annotated[
            str, typer.Option(help="Topic label for the imported article.")
        ] = "General",
        analyze: Annotated[
            bool,
            typer.Option("--analyze/--no-analyze", help="Queue local model analysis after import."),
        ] = True,
    ) -> None:
        """Collect one real article in the background, retaining source provenance."""
        _enqueue(url, "article", 1, topic, analyze)

    @app.command("ingest-feed")
    def ingest_feed(
        url: Annotated[str, typer.Argument(help="Public RSS or Atom feed URL.")],
        limit: Annotated[
            int, typer.Option(min=1, max=20, help="Maximum unique entries to fetch.")
        ] = 5,
        topic: Annotated[str, typer.Option(help="Topic label for imported articles.")] = "General",
        analyze: Annotated[
            bool,
            typer.Option("--analyze/--no-analyze", help="Queue local model analysis after import."),
        ] = True,
    ) -> None:
        """Collect a bounded feed batch; individual entry failures remain visible."""
        _enqueue(url, "feed", limit, topic, analyze)
