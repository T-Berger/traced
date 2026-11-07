"""Launch the integrated Evidence Trail workspace using existing project services."""

from __future__ import annotations

import argparse
import json
import logging
from dataclasses import replace
from pathlib import Path
from threading import Event, Thread

from ...config import Settings
from .workspace import WorkspaceRepository, WorkspaceService


def main() -> None:
    settings = Settings.from_env()
    parser = argparse.ArgumentParser(description="Evidence Trail shared review workspace")
    parser.add_argument("--data-dir", type=Path, default=settings.data_dir)
    commands = parser.add_subparsers(dest="command", required=True)
    seed = commands.add_parser(
        "seed", help="Import labeled synthetic cases and saved Explorer claims"
    )
    seed.add_argument("--without-swarm", action="store_true")
    importer = commands.add_parser("import", help="Import a portable case ZIP or evidence JSON")
    importer.add_argument("path", type=Path)
    sources = commands.add_parser("import-sources", help="Import a saved multi-platform snapshot")
    sources.add_argument("path", type=Path)
    exporter = commands.add_parser("export", help="Export a case and its complete review history")
    exporter.add_argument("case_id")
    exporter.add_argument("destination", type=Path)
    commands.add_parser("cases", help="List available review cases")
    commands.add_parser("explorer", help="Import the current saved Explorer snapshot")
    worker = commands.add_parser("worker", help="Process article and review jobs in one worker")
    worker.add_argument("--once", action="store_true")
    web = commands.add_parser("web", help="Open the integrated review desk")
    web.add_argument("--host", default="127.0.0.1")
    web.add_argument("--port", type=int, default=8053)
    web.add_argument("--with-worker", action="store_true")
    args = parser.parse_args()
    settings = replace(settings, data_dir=args.data_dir.expanduser().resolve())
    if settings.read_only:
        parser.error("This review workspace requires a writable local data directory.")
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    repository = WorkspaceRepository(settings.db_path)
    repository.initialize()
    service = WorkspaceService(repository, settings, settings.data_dir / "trail-artifacts")
    if args.command == "seed":
        from ...db import Repository
        from ...demo import seed_demo

        service.seed_examples(include_swarm=not args.without_swarm)
        articles = Repository(settings.db_path)
        articles.initialize()
        if not articles.articles.count():
            seed_demo(articles)
        service.import_explorer()
        print(json.dumps(service.list_cases(), indent=2))
    elif args.command == "cases":
        print(json.dumps(service.list_cases(), indent=2))
    elif args.command == "explorer":
        try:
            case_id = service.import_explorer()
        except ValueError as exc:
            parser.error(str(exc))
        print(case_id)
    elif args.command == "import":
        data = args.path.read_bytes()
        print(
            service.import_package(data)
            if data.startswith(b"PK")
            else service.import_bundle(json.loads(data))
        )
    elif args.command == "import-sources":
        from ..importing import MAX_IMPORT_BYTES

        if args.path.stat().st_size > MAX_IMPORT_BYTES:
            parser.error("Saved sources exceed the 5 MiB import limit.")
        content = args.path.read_bytes()
        summary = service.preview_source_import(content, args.path.name)
        print(
            service.commit_source_import(
                content, args.path.name, expected_fingerprint=summary["fingerprint"]
            )
        )
    elif args.command == "export":
        data = service.export_package(args.case_id)
        with args.destination.open("xb") as destination:
            destination.write(data)
        print(args.destination.resolve())
    elif args.command == "worker":
        from .worker import WorkspaceWorker

        runner = WorkspaceWorker(repository, settings)
        print(runner.drain() if args.once else runner.run())
    elif args.command == "web":
        from .web import create_app
        from .worker import WorkspaceWorker

        stop = Event()
        thread = None
        if args.with_worker:
            thread = Thread(
                target=WorkspaceWorker(repository, settings).run, args=(stop,), daemon=True
            )
            thread.start()
        try:
            create_app(service).run(host=args.host, port=args.port, debug=False, use_reloader=False)
        finally:
            stop.set()
            if thread:
                thread.join(timeout=2)


if __name__ == "__main__":
    main()
