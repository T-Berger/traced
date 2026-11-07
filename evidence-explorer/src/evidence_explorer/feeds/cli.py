"""Command-line adapters for durable feed subscriptions; collection stays in the worker."""

import json
from collections.abc import Callable
from typing import Annotated, Any

import typer


def _call(operation: Callable[[Any], object]) -> object:
    from ..service import ExplorerService

    try:
        return operation(ExplorerService())
    except (ValueError, PermissionError) as exc:
        typer.echo(f"Cannot update saved feeds: {exc}", err=True)
        raise typer.Exit(2) from exc


def register_feed_commands(app: typer.Typer) -> None:
    feeds = typer.Typer(no_args_is_help=True, help="Save and schedule news or public social feeds.")
    app.add_typer(feeds, name="feeds")

    @feeds.command("add")
    def add(
        name: Annotated[str, typer.Argument(help="A name for the saved feed.")],
        url: Annotated[
            str, typer.Argument(help="Public RSS URL, channel, community, or account locator.")
        ],
        every_minutes: Annotated[int, typer.Option(min=5, max=10080)] = 60,
        limit: Annotated[int, typer.Option(min=1, max=20)] = 5,
        topic: Annotated[str, typer.Option(help="Topic for collected items.")] = "General",
        platform: Annotated[str, typer.Option(help="rss, telegram, youtube, reddit, or x")] = "rss",
        social: Annotated[
            bool,
            typer.Option(help="Read post text from the feed instead of fetching article pages."),
        ] = False,
        analyze: Annotated[
            bool, typer.Option("--analyze/--no-analyze", help="Analyze imports with local Ollama.")
        ] = True,
    ) -> None:
        """Save a feed; the running worker performs its first refresh when due."""
        result = _call(
            lambda service: service.save_feed(
                name,
                url,
                topic=topic,
                interval_minutes=every_minutes,
                item_limit=limit,
                content_mode="feed_content" if social else "article_links",
                analyze=analyze,
                platform=platform,
                locator=url if platform != "rss" else None,
            )
        )
        typer.echo(json.dumps(result, indent=2, ensure_ascii=False))
        typer.echo("Keep the worker running for scheduled refreshes and local analysis.")

    @feeds.command("list")
    def list_saved(
        limit: Annotated[int, typer.Option(min=1, max=100)] = 30,
        offset: Annotated[int, typer.Option(min=0)] = 0,
    ) -> None:
        """List saved schedules and their latest import results."""
        result = _call(lambda service: service.list_feeds(limit=limit, offset=offset))
        typer.echo(json.dumps(result, indent=2, ensure_ascii=False))

    @feeds.command("analyze-stored")
    def analyze_stored(
        source_id: str,
        topic: str = "General",
        limit: Annotated[int, typer.Option(min=1, max=20)] = 20,
        analyze: Annotated[bool, typer.Option("--analyze/--no-analyze")] = True,
    ) -> None:
        """Import a Live sources snapshot into local analysis without fetching it again."""
        job = _call(
            lambda service: service.enqueue_collected_source_analysis(
                source_id,
                topic=topic,
                limit=limit,
                analyze=analyze,
            )
        )
        typer.echo(f"Queued saved-source projection job {job}; no new platform request is made.")

    @feeds.command("refresh")
    def refresh(feed_id: Annotated[int, typer.Argument(min=1)]) -> None:
        """Queue one refresh, including for a paused feed."""
        job_id = _call(lambda service: service.enqueue_feed_refresh(feed_id))
        typer.echo(f"Queued saved-feed import job {job_id}.")

    @feeds.command("pause")
    def pause(feed_id: Annotated[int, typer.Argument(min=1)]) -> None:
        """Pause future automatic refreshes while retaining saved articles."""
        _call(lambda service: service.set_feed_enabled(feed_id, False))
        typer.echo(f"Feed {feed_id} paused.")

    @feeds.command("resume")
    def resume(feed_id: Annotated[int, typer.Argument(min=1)]) -> None:
        """Resume a feed and make its next refresh due now."""
        _call(lambda service: service.set_feed_enabled(feed_id, True))
        typer.echo(f"Feed {feed_id} resumed. Keep the worker running.")
