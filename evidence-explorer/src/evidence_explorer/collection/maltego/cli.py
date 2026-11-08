"""``./traced maltego`` commands for the Maltego Data MCP data source.

Sign in with your own Maltego account, find and describe capabilities, run
explicit lookups that are frozen as hashed records in the selected workspace,
verify the records, and turn selected records into a review case through the
existing saved-source import. Tokens are never printed and never enter a record.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import webbrowser
from collections.abc import Callable
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import TextIO

from ..models import CollectionFailure
from .records import (
    MaltegoLookupRecord,
    argument_summary,
    build_source_import,
    is_access_denied,
    lookup_title,
)
from .service import LookupOutcome, MaltegoDataSource
from .settings import MaltegoSettings, MaltegoSettingsError, maltego_settings

ACCESS_DENIED_WAIT_SECONDS = 60
SourceFactory = Callable[[MaltegoSettings, Path], MaltegoDataSource]


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="traced maltego",
        description=(
            "Maltego Data MCP lookups as frozen, hashed evidence records. Lookups are explicit "
            "actions; runs spend shared team credits. Typical loop: search → describe → run → "
            "case."
        ),
    )
    commands = parser.add_subparsers(dest="command", required=True, metavar="command")
    commands.add_parser("status", help="Show settings and sign-in state without contacting Maltego")
    login = commands.add_parser("login", help="Sign in with your own Maltego account")
    login.add_argument("--no-browser", action="store_true", help="Print the sign-in address only")
    login.add_argument("--timeout", type=float, default=300.0, help="Seconds to wait (default 300)")
    commands.add_parser("logout", help="Revoke and delete this computer's saved sign-in")
    check = commands.add_parser("check", help="Connect and list the available tools")
    check.add_argument("--json", action="store_true")
    tools = commands.add_parser("tools", help="Describe the Data MCP tools and their arguments")
    tools.add_argument("name", nargs="?", help="Show only this tool")
    tools.add_argument("--json", action="store_true")

    search = commands.add_parser("search", help="Find capabilities for a goal (no credits)")
    search.add_argument("goal", help="The investigative question in one sentence")
    _entity_options(search)
    search.add_argument("--query", help="Free-text filter, e.g. whois or passive dns")
    search.add_argument("--kind", choices=("transform", "multiplexer", "any"))
    search.add_argument("--max-results", type=int, default=8)
    search.add_argument("--json", action="store_true", help="Print the full saved record")

    describe = commands.add_parser("describe", help="Show inputs, outputs, and settings")
    describe.add_argument("ids", nargs="+", metavar="id", help="ci_…, mx_…, or entity type IDs")
    describe.add_argument("--json", action="store_true", help="Print the full saved record")

    run = commands.add_parser("run", help="Run one capability on one entity (spends credits)")
    run.add_argument("capability_id", help="Transform (ci_…) or multiplexer (mx_…) ID")
    _entity_options(run)
    run.add_argument(
        "--property",
        action="append",
        default=[],
        metavar="NAME=VALUE",
        help="Input entity property (repeatable)",
    )
    run.add_argument(
        "--setting", action="append", default=[], metavar="NAME=VALUE", help="Transform setting"
    )
    run.add_argument("--max-results", type=int, help="Cap on result entities")
    run.add_argument("--impression-id", help="impression_id from the search that offered it")
    run.add_argument("--after-run-id", help="run_id that produced the input entity")
    run.add_argument("--idempotency-key", help="Reattach to an interrupted run within 24 h")
    run.add_argument("--no-describe", action="store_true", help="Skip the free describe call")
    run.add_argument("--no-wait", action="store_true", help="Return while a long run continues")
    run.add_argument(
        "--validate-only",
        action="store_true",
        help="Check entitlement, input, and settings without running or spending credits",
    )
    run.add_argument("--show", type=int, default=3000, help="Characters of result text to print")

    wait = commands.add_parser("wait", help="Resume a long run and freeze its results")
    wait.add_argument("run_id")
    wait.add_argument("--show", type=int, default=3000)

    call = commands.add_parser("call", help="Call any Data MCP tool and freeze its result")
    call.add_argument("tool", help="Exact tool name from 'traced maltego tools'")
    call.add_argument("--args", default="{}", help="Tool arguments as one JSON object")
    call.add_argument("--args-file", type=Path, help="Read the JSON arguments from a file")
    call.add_argument(
        "--retries",
        type=int,
        default=1,
        choices=range(0, 4),
        metavar="0-3",
        help="Retries after an 'Access denied' result, 60 s apart (default 1)",
    )
    call.add_argument("--show", type=int, default=2000, help="Characters of text to print")
    call.add_argument("--json", action="store_true", help="Print the full record as JSON")

    records = commands.add_parser("records", help="List saved records in the selected workspace")
    records.add_argument("--json", action="store_true")
    show = commands.add_parser("show", help="Verify and print one saved record")
    show.add_argument("record_id")
    show.add_argument("--json", action="store_true")
    commands.add_parser("verify", help="Recompute the hashes of every saved record")

    case = commands.add_parser("case", help="Create a review case from saved records")
    case.add_argument("record_ids", nargs="+", metavar="record_id")
    case.add_argument("--title", required=True, help="Case title")
    case.add_argument("--question", required=True, help="The question this case addresses")
    case.add_argument("--out", type=Path, help="Also write the saved-source JSON to a new file")
    case.add_argument(
        "--no-import", action="store_true", help="Write --out only; do not create the case"
    )
    case.add_argument(
        "--include-errors", action="store_true", help="Also keep error results as case records"
    )
    case.add_argument(
        "--include-discovery",
        action="store_true",
        help="Also keep search, describe, and validation records",
    )
    return parser


def _entity_options(command: argparse.ArgumentParser) -> None:
    command.add_argument("--type", dest="entity_type", help="Full entity type, e.g. maltego.Domain")
    command.add_argument("--value", dest="entity_value", help="Entity value, e.g. example.com")
    command.add_argument("--ref", dest="entity_ref", help="ref of an entity from an earlier result")


def _time(epoch: float | None) -> str:
    if epoch is None:
        return "no fixed expiry"
    return datetime.fromtimestamp(epoch, UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def _print_json(value: object, out: TextIO) -> None:
    out.write(json.dumps(value, ensure_ascii=False, indent=2) + "\n")


def _pairs(values: list[str], label: str) -> dict | None:
    pairs = {}
    for value in values:
        name, separator, item = value.partition("=")
        if not separator or not name.strip():
            raise CollectionFailure(f"Write each {label} as NAME=VALUE.")
        pairs[name.strip()] = item
    return pairs or None


def _schema_lines(schema: dict) -> list[str]:
    properties = schema.get("properties") if isinstance(schema.get("properties"), dict) else {}
    required = schema.get("required") if isinstance(schema.get("required"), list) else []
    lines = []
    for name, spec in properties.items():
        spec = spec if isinstance(spec, dict) else {}
        kind = spec.get("type", "any")
        if isinstance(spec.get("enum"), list):
            kind = f"{kind}: " + " | ".join(map(str, spec["enum"][:12]))
        note = spec.get("description") if isinstance(spec.get("description"), str) else ""
        flag = "required" if name in required else "optional"
        lines.append(f"    - {name} ({kind}, {flag})" + (f": {note[:300]}" if note else ""))
    return lines or ["    (no declared arguments)"]


def _record_summary(record: MaltegoLookupRecord, path: Path | None, out: TextIO) -> None:
    kinds = {
        "entities": "result entities (case evidence)",
        "discovery": "discovery or status result (audit only by default)",
        "error": "error result (kept for audit, not evidence)",
    }
    out.write(
        f"Record   {record.record_id}\n"
        f"Lookup   {lookup_title(record)}\n"
        f"Kind     {kinds[record.evidence_kind]}\n"
        f"Times    requested {record.requested_at} · completed {record.completed_at}\n"
        f"Text     {len(record.text)} characters · sha256 {record.text_sha256}\n"
    )
    if path is not None:
        out.write(f"Saved    {path}\n")
    for notice in record.notices[:10]:
        out.write(f"Notice   {notice}\n")


def _credits_line(credits: dict | None) -> str | None:
    if not credits:
        return None
    parts = []
    if "period_consumed_total" in credits:
        parts.append(f"consumed this period {credits['period_consumed_total']}")
    if "period_remaining" in credits:
        parts.append(f"remaining {credits['period_remaining']}")
    if "period_resets_at" in credits:
        parts.append(f"resets {credits['period_resets_at']}")
    estimate = " (estimate)" if credits.get("estimate") else ""
    return "Credits  " + " · ".join(parts) + estimate if parts else None


def _arguments(args: argparse.Namespace) -> dict:
    raw = args.args_file.read_text(encoding="utf-8") if args.args_file else args.args
    try:
        value = json.loads(raw)
    except (ValueError, RecursionError):
        raise CollectionFailure("Tool arguments must be valid JSON.") from None
    if not isinstance(value, dict):
        raise CollectionFailure("Tool arguments must be one JSON object, e.g. '{\"key\": 1}'.")
    return value


def _import_case(content: bytes, filename: str, data_dir: Path) -> str:
    from ...config import Settings
    from ...review.importing import MAX_IMPORT_BYTES
    from ...review.integrations.workspace import WorkspaceRepository, WorkspaceService

    if len(content) > MAX_IMPORT_BYTES:
        raise CollectionFailure("The case exceeds the 5 MiB saved-source limit; use fewer records.")
    settings = replace(Settings.from_env(), data_dir=data_dir)
    if settings.read_only:
        raise CollectionFailure("This workspace is read-only; choose a writable data directory.")
    repository = WorkspaceRepository(settings.db_path)
    repository.initialize()
    service = WorkspaceService(repository, settings, settings.data_dir / "trail-artifacts")
    try:
        summary = service.preview_source_import(content, filename)
        return service.commit_source_import(
            content, filename, expected_fingerprint=summary["fingerprint"]
        )
    except ValueError as exc:
        raise CollectionFailure(f"The case could not be imported: {exc}") from None


def _print_search(record: MaltegoLookupRecord, out: TextIO) -> None:
    payload = record.payload() or {}
    matches = payload.get("objective_matches") if isinstance(payload, dict) else None
    results = matches.get("results") if isinstance(matches, dict) else None
    out.write(f"Record   {record.record_id}\n")
    if isinstance(payload.get("impression_id"), str):
        out.write(f"Impression ID (pass to run as --impression-id): {payload['impression_id']}\n")
    resolution = payload.get("resolution")
    if resolution:
        out.write("Resolution: " + json.dumps(resolution, ensure_ascii=False)[:1500] + "\n")
    if not isinstance(results, list) or not results:
        out.write("No goal-ranked matches. Inspect the record for other facets:\n")
        out.write(record.text[:1500] + "\n")
        return
    out.write("Best capabilities for this goal (runnable catalog IDs):\n")
    for result in results:
        if not isinstance(result, dict):
            continue
        variants = result.get("target_variants") or []
        out.write(
            f"{result.get('rank', '?')}. {result.get('capability')} · score {result.get('score')}"
            f" · inputs {', '.join(map(str, result.get('input_types') or []))}\n"
        )
        for variant in variants if isinstance(variants, list) else []:
            if isinstance(variant, dict):
                out.write(
                    f"     {variant.get('catalog_id')}  {variant.get('display_name')} "
                    f"(on {variant.get('input_type')})\n"
                )
        if not variants:
            out.write("     no runnable variant accepts the held entity\n")


def _print_describe(record: MaltegoLookupRecord, out: TextIO) -> None:
    payload = record.payload() or {}
    out.write(f"Record   {record.record_id}\n")
    for item in payload.get("items") or []:
        if not isinstance(item, dict):
            continue
        outputs = item.get("output_types") if isinstance(item.get("output_types"), dict) else {}
        settings = (
            item.get("settings_schema") if isinstance(item.get("settings_schema"), dict) else {}
        )
        out.write(
            f"\n{item.get('id')} · {item.get('kind')} · {item.get('name')}\n"
            f"  {item.get('description') or ''}\n"
            f"  Inputs:  {', '.join(map(str, item.get('input_types') or []))}\n"
            f"  Outputs: {', '.join(map(str, outputs.get('effective') or []))}\n"
        )
        if settings.get("properties"):
            out.write("  Settings:\n" + "\n".join(_schema_lines(settings)) + "\n")
        requirements = item.get("input_requirements") or []
        if requirements:
            out.write("  Input requirements: " + json.dumps(requirements, ensure_ascii=False)[:800])
            out.write("\n")
    for key in ("entity_types", "not_found", "warnings"):
        if payload.get(key):
            out.write(f"{key}: {json.dumps(payload[key], ensure_ascii=False)[:800]}\n")


def _print_outcome(
    outcome: LookupOutcome, source: MaltegoDataSource, show: int, out: TextIO
) -> None:
    for record in outcome.records:
        path = source.records.path(record.record_id) if source.records else None
        _record_summary(record, path, out)
        out.write("\n")
    out.write(
        f"Run      {outcome.run_id or 'not started'} · status {outcome.status} · "
        f"{outcome.entity_count} result entities in {len(outcome.evidence)} evidence records\n"
    )
    credits = _credits_line(outcome.credits)
    if credits:
        out.write(credits + "\n")
    if outcome.idempotency_key:
        out.write(
            f"Retry    reattach without paying again: --idempotency-key {outcome.idempotency_key}\n"
        )
    for note in outcome.notes:
        out.write(f"Note     {note}\n")
    final = outcome.records[-1] if outcome.records else None
    if final is not None:
        shown = final.text[: max(0, show)]
        out.write("\n" + shown + ("\n…" if len(final.text) > len(shown) else "") + "\n")


def _run(
    args: argparse.Namespace,
    source: MaltegoDataSource,
    data_dir: Path,
    out: TextIO,
    sleep: Callable[[float], None],
) -> int:
    settings = source.settings
    say = lambda message: out.write(message + "\n")  # noqa: E731
    if args.command == "status":
        status = source.status()
        records = source.records
        out.write(
            "Maltego Data MCP\n"
            f"  Endpoint      {settings.server_url}\n"
            f"  OAuth client  {settings.client_id}\n"
            f"  Sign-in file  {settings.token_path}\n"
            f"  Sign-in       {status.message}\n"
        )
        if status.ready:
            out.write(f"  Access until  {_time(status.access_expires_at)}\n")
        if records:
            out.write(f"  Records       {records.root} ({len(records.ids())} saved)\n")
        return 0 if status.ready else 1
    if args.command == "login":
        result = source.sign_in(
            open_browser=(lambda _url: False) if args.no_browser else webbrowser.open,
            notify=say,
            timeout=args.timeout,
        )
        out.write(
            "Signed in to the Maltego Data MCP"
            + (" · access renews automatically" if result.refreshable else "")
            + f".\nThe sign-in is saved for this user only at {result.token_path}\n"
        )
        return 0
    if args.command == "logout":
        result = source.sign_out()
        if not result.removed:
            out.write("No saved Maltego sign-in was found on this computer.\n")
            return 0
        note = {True: " (revoked at Maltego)", False: " (revocation not confirmed)", None: ""}
        out.write("Signed out; the saved sign-in was deleted" + note[result.revoked] + ".\n")
        return 0
    if args.command == "check":
        check = source.check()
        server = check.server
        if args.json:
            _print_json(
                {
                    "server": {
                        "name": server.name,
                        "version": server.version,
                        "protocol_version": server.protocol_version,
                        "instructions": server.instructions,
                    },
                    "tools": [tool.name for tool in check.tools],
                },
                out,
            )
            return 0
        out.write(
            f"Connected to {server.name or 'the Data MCP'} {server.version or ''} "
            f"(MCP {server.protocol_version}).\n"
            f"{len(check.tools)} tools: {', '.join(tool.name for tool in check.tools)}\n"
        )
        if server.instructions:
            out.write("Server instructions:\n" + server.instructions[:2500] + "\n")
        return 0
    if args.command == "tools":
        tools = source.tools()
        if args.name:
            tools = [tool for tool in tools if tool.name == args.name]
            if not tools:
                raise CollectionFailure(f"The Data MCP offers no tool named {args.name!r}.")
        if args.json:
            _print_json([tool.to_json() for tool in tools], out)
            return 0
        for tool in tools:
            out.write(f"{tool.name}" + (f" — {tool.title}" if tool.title else "") + "\n")
            if tool.description:
                out.write("  " + tool.description.strip().replace("\n", "\n  ") + "\n")
            out.write("  Arguments:\n" + "\n".join(_schema_lines(tool.input_schema)) + "\n\n")
        return 0
    if args.command == "search":
        record = source.search(
            args.goal,
            entity_type=args.entity_type,
            entity_value=args.entity_value,
            entity_ref=args.entity_ref,
            query=args.query,
            kind=args.kind,
            max_results=args.max_results,
        )
        if args.json:
            _print_json(record.model_dump(mode="json"), out)
        else:
            _print_search(record, out)
        return 3 if record.is_error else 0
    if args.command == "describe":
        record = source.describe(args.ids)
        if args.json:
            _print_json(record.model_dump(mode="json"), out)
        else:
            _print_describe(record, out)
        return 3 if record.is_error else 0
    if args.command == "run":
        outcome = source.lookup(
            args.capability_id,
            entity_type=args.entity_type,
            entity_value=args.entity_value,
            entity_ref=args.entity_ref,
            entity_properties=_pairs(args.property, "property"),
            settings=_pairs(args.setting, "setting"),
            max_results=args.max_results,
            impression_id=args.impression_id,
            after_run_id=args.after_run_id,
            idempotency_key=args.idempotency_key,
            describe=not args.no_describe,
            wait=not args.no_wait,
            validate_only=args.validate_only,
            notify=say,
        )
        _print_outcome(outcome, source, args.show, out)
        if args.validate_only:
            return 0 if outcome.status == "valid" else 3
        return 0 if outcome.evidence and outcome.status not in {"error", "invalid"} else 3
    if args.command == "wait":
        outcome = source.wait_for_run(args.run_id, notify=say)
        _print_outcome(outcome, source, args.show, out)
        return 0 if outcome.evidence else 3
    if args.command == "call":
        arguments = _arguments(args)
        attempt = 0
        while True:
            record = source.run(args.tool, arguments)
            if not is_access_denied(record) or attempt >= args.retries:
                break
            attempt += 1
            out.write(
                f"The Data MCP answered 'Access denied'. Waiting {ACCESS_DENIED_WAIT_SECONDS} s "
                f"before retry {attempt} of {args.retries} (the denied attempt stays recorded).\n"
            )
            sleep(ACCESS_DENIED_WAIT_SECONDS)
        path = source.records.path(record.record_id) if source.records else None
        if args.json:
            _print_json(record.model_dump(mode="json"), out)
        else:
            _record_summary(record, path, out)
            credits = _credits_line((record.payload() or {}).get("credits"))
            if credits:
                out.write(credits + "\n")
            shown = record.text[: max(0, args.show)]
            out.write("\n" + shown + ("\n…" if len(record.text) > len(shown) else "") + "\n")
        return 3 if record.is_error else 0
    store = source.records
    if store is None:
        raise CollectionFailure("Choose a workspace data directory for saved records.")
    if args.command == "records":
        rows = store.inventory()
        if args.json:
            _print_json(rows, out)
            return 0
        if not rows:
            out.write(f"No saved Maltego records in {store.root}.\n")
        for row in rows:
            if not row["verified"]:
                out.write(f"{row['record_id']}  FAILED CHECK  {row['problem']}\n")
                continue
            out.write(
                f"{row['record_id']}  {row['completed_at']}  {row['kind']:<9}  {row['title']}\n"
            )
        return 0
    if args.command == "show":
        record = store.load(args.record_id)
        if args.json:
            _print_json(record.model_dump(mode="json"), out)
        else:
            _record_summary(record, store.path(record.record_id), out)
            out.write(f"Request  {record.tool_name} · {argument_summary(record.arguments)}\n")
            out.write("\n" + record.text + "\n")
        return 0
    if args.command == "verify":
        failures = 0
        rows = store.inventory()
        for row in rows:
            failures += not row["verified"]
            label = "verified" if row["verified"] else "FAILED: " + row["problem"]
            out.write(f"{row['record_id']}  {label}\n")
        out.write(f"{failures} of {len(rows)} saved records failed verification.\n")
        return 1 if failures else 0
    if args.command == "case":
        records = [store.load(record_id) for record_id in args.record_ids]
        source_import = build_source_import(
            records,
            title=args.title,
            question=args.question,
            include_errors=args.include_errors,
            include_discovery=args.include_discovery,
        )
        skipped = len(set(args.record_ids)) - len(source_import.items)
        content = (source_import.model_dump_json(indent=2) + "\n").encode("utf-8")
        if args.out:
            with args.out.open("xb") as handle:
                handle.write(content)
            out.write(f"Wrote saved-source JSON to {args.out.resolve()}\n")
        if skipped:
            out.write(f"Left out {skipped} search, describe, validation, or error records.\n")
        if args.no_import:
            return 0
        case_id = _import_case(content, "maltego-lookups.json", data_dir)
        out.write(
            f"Created or matched case {case_id} with {len(source_import.items)} Maltego records.\n"
        )
        return 0
    raise CollectionFailure("Unknown command.")  # pragma: no cover - argparse rejects it


def main(
    argv: list[str] | None = None,
    *,
    data_dir: Path | None = None,
    source_factory: SourceFactory | None = None,
    out: TextIO | None = None,
    sleep: Callable[[float], None] = time.sleep,
) -> int:
    out = out or sys.stdout
    args = _parser().parse_args(argv)
    if data_dir is None:
        from ...config import Settings

        data_dir = Settings.from_env().data_dir
    data_dir = Path(data_dir).expanduser().resolve()
    try:
        settings = maltego_settings()
        factory = source_factory or (
            lambda chosen, directory: MaltegoDataSource(chosen, data_dir=directory)
        )
        return _run(args, factory(settings, data_dir), data_dir, out, sleep)
    except (CollectionFailure, MaltegoSettingsError) as exc:
        sys.stderr.write(f"traced maltego: {exc}\n")
        return 2
    except FileExistsError as exc:
        sys.stderr.write(f"traced maltego: {exc.filename} already exists; choose a new file.\n")
        return 2
    except KeyboardInterrupt:
        sys.stderr.write("traced maltego: interrupted.\n")
        return 130
