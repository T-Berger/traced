"""Submit, inspect, and run external claim checks using the shared workspace."""

from __future__ import annotations

import argparse
from dataclasses import replace
from pathlib import Path

from ..config import Settings
from ..review.storage import ReviewRepository
from .contracts import CheckPolicy
from .service import ClaimCheckService


def main() -> None:
    parser = argparse.ArgumentParser(description="Evidence Trail external claim checks")
    parser.add_argument("--data-dir", type=Path, default=Path("data/evidence-trail"))
    commands = parser.add_subparsers(dest="command", required=True)
    submit = commands.add_parser(
        "submit", help="Queue external evidence search and local assessment"
    )
    submit.add_argument("case_id")
    submit.add_argument("finding_id")
    submit.add_argument("--query", action="append", default=[])
    submit.add_argument(
        "--source", action="append", default=[], help="Optional original source URL"
    )
    submit.add_argument("--max-sources", type=int, default=6)
    show = commands.add_parser("show", help="Inspect the durable check, evidence, and result")
    show.add_argument("job_id")
    listing = commands.add_parser("list", help="List recent checks in a case")
    listing.add_argument("case_id")
    retry = commands.add_parser("retry", help="Retry a failed check using its frozen evidence")
    retry.add_argument("job_id")
    commands.add_parser("run", help="Drain queued work through the shared workspace worker")
    args = parser.parse_args()
    settings = replace(Settings.from_env(), data_dir=args.data_dir.expanduser().resolve())
    repository = ReviewRepository(settings.db_path)
    service = ClaimCheckService(repository, settings)
    if args.command == "submit":
        job = service.submit(
            args.case_id,
            args.finding_id,
            CheckPolicy(queries=args.query, seed_urls=args.source, max_sources=args.max_sources),
        )
        print(job.model_dump_json(indent=2))
    elif args.command == "show":
        job = service.get(args.job_id)
        if job is None:
            parser.error("Unknown claim-check job.")
        print(job.model_dump_json(indent=2))
    elif args.command == "list":
        for job in service.list(args.case_id):
            print(job.model_dump_json())
    elif args.command == "retry":
        print(service.retry(args.job_id).model_dump_json(indent=2))
    else:
        from ..review.integrations.worker import WorkspaceWorker

        print(f"Processed jobs: {WorkspaceWorker(repository, settings).drain()}")


if __name__ == "__main__":
    main()
