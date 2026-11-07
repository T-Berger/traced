"""Explicit local data preparation and application commands."""

import argparse
import json
import os
import sys
from pathlib import Path

from .service import Workbench


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Swarm Detector: inspect behavioural evidence locally."
    )
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("demo", help="Initialize the synthetic demonstration in an empty database.")
    serve = commands.add_parser("serve", help="Open the local analyst application.")
    serve.add_argument("--host", default=os.environ.get("SWARM_HOST", "127.0.0.1"))
    serve.add_argument("--port", type=int, default=int(os.environ.get("PORT", "8050")))
    inspect = commands.add_parser(
        "inspect-archive", help="List completed ZIP metadata without extraction."
    )
    inspect.add_argument("path")
    ingest = commands.add_parser(
        "import-jsonl", help="Validate and import the documented normalized format."
    )
    ingest.add_argument("path")
    analyze = commands.add_parser(
        "analyze", help="Save a deterministic analysis of the active imported dataset."
    )
    analyze.add_argument("--window", type=int, default=120)
    analyze.add_argument("--controls", type=int, default=20)
    analyze.add_argument("--seed", type=int, default=42)
    export = commands.add_parser(
        "export", help="Export a selected case and its supporting records."
    )
    export.add_argument("case_id")
    export.add_argument("--format", choices=("markdown", "json", "csv"), default="markdown")
    export.add_argument("--output")
    backup = commands.add_parser("backup", help="Create a consistent SQLite backup at a new path.")
    backup.add_argument("path")
    args = parser.parse_args(argv)
    try:
        if args.command == "inspect-archive":
            from .ingest import inspect_archive

            print(json.dumps(inspect_archive(args.path), ensure_ascii=False, indent=2))
            return 0
        wb = Workbench()
        if args.command == "demo":
            print(json.dumps(wb.ensure_sample(), ensure_ascii=False, indent=2))
        elif args.command == "serve":
            from .web import create_app

            app = create_app(wb)
            app.run(host=args.host, port=args.port, debug=False)
        elif args.command == "import-jsonl":
            from .ingest import load_jsonl

            dataset = load_jsonl(args.path)
            wb.store.put_dataset(dataset)
            print(
                json.dumps(
                    {
                        "dataset_id": dataset["dataset_id"],
                        "manifest": dataset["manifest"],
                        "next_step": "Run swarm-detector analyze to create a saved analysis.",
                    },
                    indent=2,
                )
            )
        elif args.command == "analyze":
            run = wb.run_analysis(
                {"window_seconds": args.window, "randomizations": args.controls, "seed": args.seed}
            )
            print(
                json.dumps(
                    {
                        "run_id": run["run_id"],
                        "candidate_groups": len(run["groups"]),
                        "diagnostics": run["diagnostics"],
                    },
                    indent=2,
                )
            )
        elif args.command == "export":
            content = wb.export_case(args.case_id, args.format)
            if args.output:
                target = Path(args.output).expanduser()
                target.parent.mkdir(parents=True, exist_ok=True)
                with target.open("x", encoding="utf-8", newline="") as output:
                    output.write(content)
                print(str(target.resolve()))
            else:
                print(content)
        elif args.command == "backup":
            print(str(wb.store.backup(args.path)))
        return 0
    except (ValueError, OSError) as exc:
        print(f"Unable to complete command: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
