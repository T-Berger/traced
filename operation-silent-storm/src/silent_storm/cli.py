"""Local import, inspection, checksumming, and server commands."""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path

from .config import AppConfig


def build_parser() -> argparse.ArgumentParser:
    defaults = AppConfig.from_env()
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--db", type=Path, default=defaults.db_path, help="Local SQLite database")
    parser = argparse.ArgumentParser(prog="silent-storm", description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    importer = commands.add_parser("import", parents=[common], help="Validate and import metadata")
    importer.add_argument("--dataset", type=Path, default=defaults.dataset_root)
    importer.add_argument("--release-id", default="day-1")
    importer.add_argument("--title", default="Day 1")
    hashes = commands.add_parser("hash-media", parents=[common], help="Resume media SHA-256 checks")
    hashes.add_argument("--release-id", default="day-1")
    hashes.add_argument("--limit", type=int, help="Maximum files to attempt in this invocation")
    stats = commands.add_parser("stats", parents=[common], help="Show imported release coverage")
    stats.add_argument("--release-id", default="day-1")
    serve = commands.add_parser("serve", parents=[common], help="Run the local Evidence Explorer")
    serve.add_argument("--dataset", type=Path, default=defaults.dataset_root)
    serve.add_argument("--host", default=defaults.host)
    serve.add_argument("--port", type=int, default=defaults.port)
    serve.add_argument("--no-import", action="store_true", help="Do not import Day 1 on first run")
    serve.add_argument("--debug", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    from .ingest import hash_media, import_release
    from .storage import Store

    try:
        store = Store(args.db)
        if args.command == "import":
            result = import_release(store, args.dataset, args.release_id, args.title)
        elif args.command == "hash-media":
            if args.limit is not None and args.limit < 1:
                raise ValueError("The checksum limit must be positive.")
            result = hash_media(store, args.release_id, limit=args.limit)
        elif args.command == "stats":
            if store.get_release(args.release_id) is None:
                raise ValueError("Release not found. Import the dataset first.")
            result = store.overview(args.release_id)
        else:
            from .app import create_app

            if not 1 <= args.port <= 65535:
                raise ValueError("Port must be between 1 and 65535.")
            dataset = args.dataset.expanduser().resolve()
            if not args.no_import and not store.list_releases() and dataset.is_dir():
                report = import_release(store, dataset, "day-1", "Day 1")
                print(json.dumps(report, ensure_ascii=False, indent=2))
            settings = AppConfig(
                db_path=args.db.expanduser().resolve(),
                dataset_root=dataset,
                host=args.host,
                port=args.port,
            )
            app = create_app(store=store, config=settings)
            app.run(host=settings.host, port=settings.port, debug=args.debug, use_reloader=False)
            return 0
    except (ValueError, OSError, sqlite3.Error, KeyError) as error:
        print(f"Unable to complete the request: {error}", file=sys.stderr)
        return 1
    print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
    return 0
