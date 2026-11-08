"""Read-only Supabase setup diagnostics; collection stays in the shared workflow."""

from __future__ import annotations

import argparse
import json
import sys

from .models import CollectionFailure
from .supabase import configuration_status, inspect_table


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="traced supabase",
        description="Check Supabase settings and table columns without collecting content.",
    )
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("status", help="Report configuration readiness without connecting")
    check = commands.add_parser(
        "check", help="Read table column metadata using a read-only connection"
    )
    check.add_argument(
        "--table", default="public.content_items", help="Schema-qualified table name"
    )
    args = parser.parse_args(argv)

    try:
        if args.command == "status":
            status = configuration_status()
            # Do not expose future configuration fields, connection strings, or credentials.
            result = {"ready": bool(status["ready"]), "message": status["message"]}
            print(json.dumps(result, indent=2))
            return 0 if result["ready"] else 1
        columns = inspect_table(args.table)
        print(json.dumps({"status": "connected", "columns": columns}, indent=2))
        return 0
    except CollectionFailure as exc:
        # The adapter's CollectionFailure contract contains only safe diagnostic text.
        print(str(exc), file=sys.stderr)
    except Exception:
        # A driver or an unexpected failure can include the password in its message.
        print(
            "Supabase could not be checked. Verify the server settings, table, permissions, "
            "and network connection.",
            file=sys.stderr,
        )
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
