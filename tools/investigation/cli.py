"""Command line for show-your-work investigations: ``./traced-investigate``.

start   create an investigation; search and describe capabilities (free)
lookup  run named capabilities on the subject (spends credits), or attach
        records already frozen in this workspace (free)
write   have the model draft claims from the evidence records; build bundle.json
check   check a bundle; write report.json, report.md, checked-bundle.json
plant   copy an investigation with one planted assumption (checker demo)
show    print a saved check report
run     start, lookup, write, and check in one go (spends credits)
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
from pathlib import Path

from evidence_explorer.collection.maltego.records import RecordStore
from evidence_explorer.collection.models import CollectionFailure
from evidence_explorer.review.verifier import (
    CheckError,
    OpenRouterVerifier,
    openrouter_configuration,
)

from .bundle import InvestigationDraft
from .checker import check_bundle
from .demo import plant_assumption
from .investigator import Investigation, now, read_json, write_json
from .model import OpenRouterModel
from .reasoning import Reasoner
from .render import render_markdown, render_summary


def _say(message: str) -> None:
    print(message, file=sys.stderr, flush=True)


def _folder(value: str, data_dir: Path) -> Path:
    path = Path(value).expanduser()
    if path.exists():
        return path.resolve()
    candidate = data_dir / "investigations" / value
    if candidate.exists():
        return candidate.resolve()
    raise FileNotFoundError(f"No investigation folder or file named {value}.")


def _source(data_dir: Path):
    from evidence_explorer.collection.maltego.service import MaltegoDataSource

    return MaltegoDataSource(data_dir=data_dir)


def _reasoner(choice: str, model: str | None, cache: Path | None) -> Reasoner:
    """The model verifier, or a reasoner that records exactly why it did not run."""
    if choice == "off":
        return Reasoner(None, cache_path=cache, not_run_reason="turned off with --reasoning off")
    missing = [
        name
        for name in openrouter_configuration()["missing"]
        if not (model and name == "EVIDENCE_REVIEW_MODEL")
    ]
    if choice == "auto" and missing:
        reason = "OpenRouter is not configured (" + " and ".join(missing) + " not set)"
        _say(f"Model checks not run: {reason}. Mechanical and deterministic checks still run.")
        return Reasoner(None, cache_path=cache, not_run_reason=reason)
    return Reasoner(OpenRouterVerifier.from_env(model), cache_path=cache)


def _print_candidates(investigation: Investigation) -> None:
    plan = investigation.manifest.get("plan") or {}
    print(f"Investigation: {investigation.folder}")
    print(
        f"Goal: {investigation.goal} · subject {investigation.subject['entity_type']} "
        f"{investigation.subject['entity_value']}"
    )
    for candidate in plan.get("candidates") or []:
        described = candidate.get("described") or {}
        outputs = ", ".join(described.get("output_types") or []) if described else "not described"
        settings = described.get("required_settings") if described else None
        extra = f" · needs settings {settings}" if settings else ""
        print(
            f"  {candidate['rank']:>2}. {candidate['capability_id']}  {candidate['name']}  → {outputs}{extra}"
        )
    if plan.get("impression_id"):
        print(f"Impression ID {plan['impression_id']} (sent with runs, as the Data MCP asks).")


def cmd_start(args: argparse.Namespace, data_dir: Path) -> int:
    investigation = Investigation.create(
        data_dir / "investigations", args.goal, args.type, args.value
    )
    if args.offline:
        print(investigation.folder)
        return 0
    source = _source(data_dir)
    investigation.plan(source, max_results=args.max_results, describe=args.describe)
    _print_candidates(investigation)
    if args.validate:
        ids = [
            c["capability_id"]
            for c in investigation.manifest["plan"]["candidates"][: args.validate]
        ]
        for result in investigation.validate(source, ids):
            print(
                f"  validate {result['capability_id']}: {result['status']}"
                + (" (error)" if result["is_error"] else "")
            )
    return 0


def cmd_lookup(args: argparse.Namespace, data_dir: Path) -> int:
    investigation = Investigation.open(_folder(args.folder, data_dir))
    store = RecordStore(data_dir)
    if args.record:
        records = investigation.attach(store, args.record)
        print(f"Attached {len(records)} saved record(s); no lookup ran and no credits were used.")
    if not args.capability:
        return 0
    source = _source(data_dir)
    if args.validate_only:
        for result in investigation.validate(source, args.capability):
            print(
                f"validate {result['capability_id']}: {result['status']}"
                + (" (error)" if result["is_error"] else "")
                + f" · record {str(result['record_id'])[:12]}…"
            )
        return 0
    for entry in investigation.lookup(
        source, args.capability, max_results=args.max_results, notify=_say
    ):
        credits = entry["credits"] or {}
        print(
            f"{entry['capability_id']} {entry['capability_name'] or ''}: {entry['status']} · "
            f"{entry['entities']} entities · {len(entry['evidence_record_ids'])} evidence record(s) · "
            f"credits consumed this period {credits.get('period_consumed_total', '?')}, "
            f"remaining {credits.get('period_remaining', '?')}"
        )
        for note in entry["notes"]:
            print(f"  note: {note}")
    return 0


def cmd_write(args: argparse.Namespace, data_dir: Path) -> int:
    investigation = Investigation.open(_folder(args.folder, data_dir))
    if (investigation.folder / "bundle.json").exists() and not args.replace:
        raise FileExistsError(
            f"{investigation.folder / 'bundle.json'} exists; pass --replace to overwrite it "
            "(earlier reports stay pinned to the old bundle fingerprint), or start a new investigation."
        )
    store = RecordStore(data_dir)
    if args.draft:
        draft = InvestigationDraft.model_validate_json(args.draft.read_text(encoding="utf-8"))
        summary = investigation.write(
            store, draft=draft, draft_source=f"file:{args.draft.name}", draft_note=args.draft_note
        )
    else:
        model = OpenRouterModel.from_env(args.model)
        summary = investigation.write(store, model=model, max_claims=args.max_claims)
    print(
        f"Wrote {investigation.folder / 'bundle.json'}: {summary['claims']} claims, "
        f"{summary['assumptions']} assumptions, {summary['citations']} citations "
        f"({summary['rejected_citations']} rejected)."
    )
    for problem in summary["problems"]:
        print(f"  note: {problem}")
    return 0


def check_target(
    target: Path, data_dir: Path, reasoning: str, model: str | None, out: Path | None
) -> dict:
    if target.is_dir():
        bundle_path, out_dir = target / "bundle.json", out or target / "check"
    else:
        bundle_path, out_dir = target, out or target.with_name(target.stem + "-check")
    data = read_json(bundle_path)
    reasoner = _reasoner(reasoning, model, out_dir / "reasoning-cache.json")
    report = check_bundle(data, store=RecordStore(data_dir), reasoner=reasoner)
    payload = report.to_json()
    write_json(out_dir / "report.json", payload)
    (out_dir / "report.md").write_text(render_markdown(payload), encoding="utf-8")
    if report.checked_bundle is not None:
        write_json(out_dir / "checked-bundle.json", report.checked_bundle)
    if target.is_dir() and (target / "investigation.json").is_file():
        investigation = Investigation.open(target)
        report_path = out_dir / "report.json"
        investigation.manifest["checks"].append(
            {
                "at": payload["checked_at"],
                "report": str(report_path.relative_to(target))
                if report_path.is_relative_to(target)
                else str(report_path),
                "reasoning": payload["checker"]["reasoning"],
                "summary": {k: payload["summary"][k] for k in ("links", "conclusions")},
            }
        )
        investigation.save()
    payload["_out_dir"] = str(out_dir)
    return payload


def cmd_check(args: argparse.Namespace, data_dir: Path) -> int:
    payload = check_target(
        _folder(args.target, data_dir), data_dir, args.reasoning, args.model, args.out
    )
    out_dir = payload.pop("_out_dir")
    if args.json:
        print(json.dumps(payload, indent=2, ensure_ascii=False))
    else:
        print(render_summary(payload))
        print(f"Report: {out_dir}/report.md")
    return 0


def cmd_plant(args: argparse.Namespace, data_dir: Path) -> int:
    source = _folder(args.folder, data_dir)
    bundle = read_json(source / "bundle.json")
    planted, key = plant_assumption(
        bundle, statement=args.statement, into=args.into, created_at=now()
    )
    out = args.out or source.with_name(source.name + "-planted")
    if out.exists():
        raise FileExistsError(f"{out} already exists; choose --out.")
    out.mkdir(parents=True, mode=0o700)
    for name in ("investigation.json", "draft.json", "citations.json"):
        if (source / name).is_file():
            shutil.copy2(source / name, out / name)
    write_json(out / "bundle.json", planted)
    write_json(out / "planted.json", key)
    if (out / "investigation.json").is_file():
        investigation = Investigation.open(out)
        investigation.manifest["checks"] = []
        investigation.manifest["planted_from"] = source.name
        investigation.manifest["bundle"] = {"path": "bundle.json", "planted": True}
        investigation.log("plant", label=key["planted"][0]["label"], into=args.into)
    print(f"Planted {key['planted'][0]['label']} into {args.into}: {out}")
    print("The answer key is in planted.json; the checker does not read it.")
    return 0


def cmd_show(args: argparse.Namespace, data_dir: Path) -> int:
    target = _folder(args.target, data_dir)
    path = target / "check" / "report.json" if target.is_dir() else target
    payload = read_json(path)
    print(render_markdown(payload) if args.markdown else render_summary(payload))
    return 0


def cmd_run(args: argparse.Namespace, data_dir: Path) -> int:
    if not args.capability and not args.record and not args.auto:
        raise ValueError(
            "Name the capabilities to run (--capability), saved records (--record), or --auto N."
        )
    investigation = Investigation.create(
        data_dir / "investigations", args.goal, args.type, args.value
    )
    store = RecordStore(data_dir)
    if args.record:
        investigation.attach(store, args.record)
    capabilities = list(args.capability)
    if args.auto or capabilities:
        source = _source(data_dir)
        candidates = investigation.plan(
            source, max_results=max(8, args.auto), describe=max(4, args.auto)
        )
        capabilities += [
            c.capability_id for c in candidates[: args.auto] if c.capability_id not in capabilities
        ]
        _print_candidates(investigation)
        for entry in investigation.lookup(
            source, capabilities, max_results=args.max_results, notify=_say
        ):
            credits = entry["credits"] or {}
            print(
                f"ran {entry['capability_id']}: {entry['status']} · credits remaining "
                f"{credits.get('period_remaining', '?')}"
            )
    model = OpenRouterModel.from_env(args.model)
    investigation.write(store, model=model, max_claims=args.max_claims)
    payload = check_target(investigation.folder, data_dir, args.reasoning, args.model, None)
    payload.pop("_out_dir")
    print(render_summary(payload))
    print(f"Investigation: {investigation.folder}")
    return 0


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(
        prog="traced-investigate",
        description="Show-your-work investigations over frozen Maltego Data MCP records.",
    )
    root.add_argument("--data-dir", help="Explicit store, relative to the EDTH root or absolute")
    sub = root.add_subparsers(dest="command", required=True)

    start = sub.add_parser("start", help="create an investigation and plan lookups (free)")
    start.add_argument("goal", help="the investigative question in one sentence")
    start.add_argument("--type", required=True, help="Maltego entity type, e.g. maltego.Domain")
    start.add_argument("--value", required=True, help="entity value, e.g. example.org")
    start.add_argument("--max-results", type=int, default=8)
    start.add_argument(
        "--describe", type=int, default=4, help="describe the top N candidates (free)"
    )
    start.add_argument(
        "--validate", type=int, default=0, help="validate-only check of the top N (free)"
    )
    start.add_argument(
        "--offline", action="store_true", help="create the folder without calling Maltego"
    )

    lookup = sub.add_parser(
        "lookup", help="run capabilities (spends credits) or attach saved records"
    )
    lookup.add_argument("folder")
    lookup.add_argument(
        "--capability", action="append", default=[], help="ci_… or mx_… ID (repeatable)"
    )
    lookup.add_argument(
        "--record", action="append", default=[], help="saved record ID to attach (free)"
    )
    lookup.add_argument("--max-results", type=int, default=25)
    lookup.add_argument(
        "--validate-only", action="store_true", help="check without executing (free)"
    )

    write = sub.add_parser("write", help="draft claims and build bundle.json")
    write.add_argument("folder")
    write.add_argument("--model", help="OpenRouter model (default: EVIDENCE_REVIEW_MODEL)")
    write.add_argument(
        "--draft", type=Path, help="use a hand-written draft JSON instead of a model"
    )
    write.add_argument("--max-claims", type=int, default=4)
    write.add_argument("--replace", action="store_true", help="overwrite an existing bundle.json")
    write.add_argument("--draft-note", help="who wrote a --draft file and why (kept in the bundle)")

    check = sub.add_parser("check", help="check a bundle and write the reports")
    check.add_argument("target", help="investigation folder or bundle JSON file")
    check.add_argument("--reasoning", choices=("auto", "openrouter", "off"), default="auto")
    check.add_argument("--model", help="OpenRouter model for the reasoning checks")
    check.add_argument("--out", type=Path, help="output folder (default: <folder>/check)")
    check.add_argument("--json", action="store_true", help="print the full JSON report")

    plant = sub.add_parser("plant", help="copy an investigation with one planted assumption")
    plant.add_argument("folder")
    plant.add_argument("--statement", required=True, help="the planted assumption")
    plant.add_argument("--into", required=True, help="label of the claim or step that relies on it")
    plant.add_argument("--out", type=Path)

    show = sub.add_parser("show", help="print a saved check report")
    show.add_argument("target")
    show.add_argument("--markdown", action="store_true")

    run = sub.add_parser("run", help="start, lookup, write, and check (spends credits)")
    run.add_argument("goal")
    run.add_argument("--type", required=True)
    run.add_argument("--value", required=True)
    run.add_argument("--capability", action="append", default=[])
    run.add_argument("--record", action="append", default=[])
    run.add_argument("--auto", type=int, default=0, help="also run the top N search candidates")
    run.add_argument("--max-results", type=int, default=25)
    run.add_argument("--max-claims", type=int, default=4)
    run.add_argument("--model")
    run.add_argument("--reasoning", choices=("auto", "openrouter", "off"), default="auto")
    return root


COMMANDS = {
    "start": cmd_start,
    "lookup": cmd_lookup,
    "write": cmd_write,
    "check": cmd_check,
    "plant": cmd_plant,
    "show": cmd_show,
    "run": cmd_run,
}


def main(argv: list[str] | None, *, data_dir: Path) -> int:
    args = parser().parse_args(argv)
    try:
        return COMMANDS[args.command](args, data_dir)
    except (CollectionFailure, CheckError, ValueError, FileNotFoundError, FileExistsError) as exc:
        _say(f"traced-investigate: {exc}")
        return 2


def parse_data_dir(argv: list[str]) -> tuple[str | None, list[str]]:
    """Split the global --data-dir option from the command arguments."""
    probe = argparse.ArgumentParser(add_help=False)
    probe.add_argument("--data-dir")
    namespace, rest = probe.parse_known_args(argv)
    return namespace.data_dir, rest


def entry(argv: list[str] | None = None, *, root: Path) -> int:
    """Resolve the workspace exactly like ``./traced``, then run a command."""
    sys.path.insert(0, str(root))
    from tools.traced import configure_environment

    arguments = list(sys.argv[1:] if argv is None else argv)
    data_dir, rest = parse_data_dir(arguments)
    configuration = configure_environment(root, data_dir)
    return main(rest, data_dir=Path(configuration["data_dir"]))
