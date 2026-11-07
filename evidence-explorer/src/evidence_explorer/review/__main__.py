"""Run with: uv run python -m evidence_explorer.review web --with-worker."""

from __future__ import annotations

import argparse
import json
import logging
import os
from pathlib import Path
from threading import Event, Thread

from dotenv import load_dotenv


def main() -> None:
    load_dotenv(override=False)
    parser = argparse.ArgumentParser(description="Evidence Trail review workspace")
    parser.add_argument(
        "--db",
        type=Path,
        default=Path(os.getenv("EVIDENCE_REVIEW_DB", "data/evidence.sqlite3")),
    )
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("seed", help="Import the labelled synthetic Harbor case")
    importer = commands.add_parser("import", help="Validate and import an evidence bundle")
    importer.add_argument("path", type=Path)
    web = commands.add_parser("web", help="Start the review interface")
    web.add_argument("--host", default="127.0.0.1")
    web.add_argument("--port", type=int, default=int(os.getenv("PORT", "8052")))
    web.add_argument("--with-worker", action="store_true")
    worker = commands.add_parser("worker", help="Run queued review checks")
    worker.add_argument("--once", action="store_true", help="Drain queued checks and exit")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    from .service import ReviewService
    from .storage import ReviewRepository
    from .worker import ReviewWorker

    repository = ReviewRepository(args.db)
    repository.initialize()
    service = ReviewService(repository)
    if args.command == "seed":
        print(service.seed_demo())
    elif args.command == "import":
        print(service.import_bundle(json.loads(args.path.read_text(encoding="utf-8"))))
    elif args.command == "worker":
        runner = ReviewWorker(repository)
        if args.once:
            while runner.run_once():
                pass
        else:
            runner.run()
    elif args.command == "web":
        from .web import create_app

        if not service.list_cases():
            service.seed_demo()
        app = create_app(service)
        stop = Event()
        thread = None
        if args.with_worker:
            thread = Thread(target=ReviewWorker(repository).run, args=(stop,), daemon=True)
            thread.start()
        try:
            app.run(host=args.host, port=args.port, debug=False, use_reloader=False)
        finally:
            stop.set()
            if thread:
                thread.join(timeout=2)


if __name__ == "__main__":
    main()
