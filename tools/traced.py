"""Root entry point for the existing integrated Traced application.

Keep configuration and command dispatch here; evidence, storage, and worker
behavior belong to evidence_explorer.review.integrations.
"""

from __future__ import annotations

import argparse
import json
import os
import runpy
import sys
from pathlib import Path

from dotenv import load_dotenv

REVIEW_MODULE = "evidence_explorer.review.integrations"


def configure_environment(root: Path, data_dir: str | None = None) -> dict[str, str]:
    """Resolve one store without opening it or moving any saved data.

    Existing process/Explorer settings keep their Explorer-relative meaning.
    New root .env and --data-dir values are relative to the common root.
    """
    root = root.resolve()
    explorer = root / "evidence-explorer"
    source = "process environment" if "EVIDENCE_DATA_DIR" in os.environ else "default"
    base = explorer
    dotenv_disabled = os.getenv("PYTHON_DOTENV_DISABLED", "").lower() in {
        "1",
        "true",
        "t",
        "yes",
        "y",
    }
    if not dotenv_disabled:
        load_dotenv(root / ".env", override=False)
        if source == "default" and "EVIDENCE_DATA_DIR" in os.environ:
            source, base = "root .env", root
        load_dotenv(explorer / ".env", override=False)
        if source == "default" and "EVIDENCE_DATA_DIR" in os.environ:
            source = "evidence-explorer/.env"

    selected = os.getenv("EVIDENCE_DATA_DIR", "data")
    if data_dir is not None:
        selected, base, source = data_dir, root, "--data-dir"
    if not selected.strip():
        raise ValueError(
            "EVIDENCE_DATA_DIR / --data-dir must name a directory, not an empty value."
        )
    path = Path(selected).expanduser()
    path = (base / path).resolve() if not path.is_absolute() else path.resolve()
    os.environ["EVIDENCE_DATA_DIR"] = str(path)
    # Settings and providers must use this captured configuration, not discover
    # another .env from a package, caller, or parent directory later in startup.
    os.environ["PYTHON_DOTENV_DISABLED"] = "1"
    # Flask reads dotenv_values directly, bypassing python-dotenv's disable flag.
    # Do not let app.run discover a parent project's credentials after selection.
    os.environ["FLASK_SKIP_DOTENV"] = "1"
    return {
        "project_root": str(root),
        "environment": str(root / ".venv"),
        "data_dir": str(path),
        "database": str(path / "evidence.sqlite3"),
        "data_source": source,
    }


def _run_review(root: Path, arguments: list[str]) -> None:
    previous_cwd, previous_argv = Path.cwd(), sys.argv
    try:
        os.chdir(root)
        sys.argv = [REVIEW_MODULE, *arguments]
        runpy.run_module(REVIEW_MODULE, run_name="__main__")
    finally:
        sys.argv = previous_argv
        os.chdir(previous_cwd)


def main(argv: list[str] | None = None, *, root: Path | None = None) -> None:
    root = (root or Path(__file__).resolve().parents[1]).resolve()
    parser = argparse.ArgumentParser(
        prog="traced",
        description="Traced: shared evidence workbench",
        epilog=(
            "start (default): integrated desk and combined worker; "
            "config: show paths without opening a database; "
            "review: forward to the existing review CLI; "
            "maltego: sign in to the Maltego Data MCP and run explicit lookups; "
            "supabase: check the read-only Postgres source. Use '<command> --help' for details."
        ),
    )
    parser.add_argument("--data-dir", help="Explicit store, relative to the EDTH root or absolute")
    parser.add_argument(
        "command",
        choices=("start", "config", "review", "maltego", "supabase"),
        nargs="?",
        default="start",
    )
    parser.add_argument("arguments", nargs=argparse.REMAINDER)
    args = parser.parse_args(argv)
    start_args = None
    start = argparse.ArgumentParser(prog="traced start", description="Start the integrated desk")
    start.add_argument("--host", help="Listening address (default: EVIDENCE_HOST or 127.0.0.1)")
    start.add_argument("--port", type=int, help="Listening port (default: TRACED_PORT or 8053)")
    start.add_argument("--no-worker", action="store_true", help="Serve the desk without a worker")
    if args.command == "start":
        start_args = start.parse_args(args.arguments)
    elif args.command == "config":
        argparse.ArgumentParser(
            prog="traced config", description="Show selected paths without opening a database"
        ).parse_args(args.arguments)
    if args.command == "review" and not args.arguments:
        parser.error("review requires a command; use './traced review --help'")
    try:
        configuration = configure_environment(root, args.data_dir)
    except ValueError as exc:
        parser.error(str(exc))
    if args.command == "config":
        print(json.dumps(configuration, indent=2))
        return
    if args.command == "review":
        _run_review(root, args.arguments)
        return
    if args.command == "maltego":
        from evidence_explorer.collection.maltego.cli import main as maltego_main

        raise SystemExit(maltego_main(args.arguments, data_dir=Path(configuration["data_dir"])))
    if args.command == "supabase":
        from evidence_explorer.collection.supabase_cli import main as supabase_main

        raise SystemExit(supabase_main(args.arguments))
    assert start_args is not None
    host = start_args.host or os.getenv("EVIDENCE_HOST", "127.0.0.1")
    try:
        port = start_args.port
        if port is None:
            port = int(os.getenv("TRACED_PORT", "8053"))
        if not 1 <= port <= 65535:
            raise ValueError
    except ValueError:
        parser.error("TRACED_PORT / --port must be an integer between 1 and 65535.")
    arguments = ["web", "--host", host, "--port", str(port)]
    if not start_args.no_worker:
        arguments.append("--with-worker")
    _run_review(root, arguments)


if __name__ == "__main__":
    main()
