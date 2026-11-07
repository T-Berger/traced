"""Bounded investigator actions over the existing saved review workspace.

Browsing and checking only read saved evidence. A check runs after its SQLite
snapshot closes and returns a report for that exact assessment, never a decision.
Draft saves and imports are explicit actions using the existing immutable merger.
"""

from __future__ import annotations

import json
import os
import re
import stat
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path

from ..collection.maltego.records import MaltegoLookupRecord, RecordStore
from ..review.contracts import EvidenceBundle
from ..review.integrations.snapshots import workspace_snapshot
from ..review.integrations.workspace import WorkspaceRepository, WorkspaceService
from ..review.navigation import navigation_context
from ..review.workflow import assessment_context
from .bundle import INVESTIGATION_METHOD, InvestigationDraft, build_saved_investigation_bundle
from .checker import LABELS, FrozenRecordUnavailable, check_bundle
from .reasoning import Reasoner

MAX_SAVED_ENTRIES = 100
MAX_SCAN_ENTRIES = 300
MAX_BUNDLE_BYTES = 8 * 1024 * 1024
MAX_MANIFEST_BYTES = 256 * 1024
MAX_CHECK_TEXT_BYTES = 1024 * 1024
MAX_FROZEN_BYTES = 2 * 1024 * 1024
MAX_FROZEN_TOTAL_BYTES = 8 * 1024 * 1024
MAX_CHECK_TRAVERSALS = 20_000
CHECK_LIMITS = {"records": 100, "findings": 300, "fragments": 1000, "links": 1000, "runs": 50}
_FOLDER_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,199}\Z")
_UNAVAILABLE = "This saved investigation is unavailable or exceeds the supported import limits."
_CHANGED = "The saved evidence or assessment changed. Refresh it before continuing."


@contextmanager
def _directory(root: Path, *parts: str) -> Iterator[int]:
    """Pin each directory without following child symlinks or reopening paths."""
    descriptor = os.open(root, os.O_RDONLY | os.O_DIRECTORY)
    try:
        for part in parts:
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = child
        yield descriptor
    finally:
        os.close(descriptor)


def _read_file(directory: int, name: str, limit: int) -> bytes:
    descriptor = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
    with os.fdopen(descriptor, "rb") as stream:
        metadata = os.fstat(stream.fileno())
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_size > limit:
            raise ValueError(_UNAVAILABLE)
        content = stream.read(limit + 1)
        if len(content) > limit:
            raise ValueError(_UNAVAILABLE)
    return content


def _root(service: WorkspaceService) -> Path:
    return Path(service.settings.data_dir).expanduser().resolve()


def _load_saved(service: WorkspaceService, investigation_id: str) -> tuple[dict, dict]:
    if not isinstance(investigation_id, str) or not _FOLDER_ID.fullmatch(investigation_id):
        raise ValueError(_UNAVAILABLE)
    try:
        with _directory(_root(service), "investigations", investigation_id) as directory:
            manifest = json.loads(_read_file(directory, "investigation.json", MAX_MANIFEST_BYTES))
            saved = EvidenceBundle.model_validate_json(
                _read_file(directory, "bundle.json", MAX_BUNDLE_BYTES)
            )
        if not isinstance(manifest, dict) or manifest.get("schema") != "traced.investigation/1":
            raise ValueError(_UNAVAILABLE)
        recorded = manifest.get("bundle")
        if (
            not isinstance(recorded, dict)
            or recorded.get("path") != "bundle.json"
            or recorded.get("fingerprint") != saved.fingerprint()
        ):
            raise ValueError(_UNAVAILABLE)
        bundle = saved.model_dump(mode="json")
        runs = [run for run in bundle["runs"] if run["method"] == INVESTIGATION_METHOD]
        if not runs:
            raise ValueError(_UNAVAILABLE)
        return bundle, runs[-1]
    except (OSError, ValueError, TypeError, RecursionError):
        raise ValueError(_UNAVAILABLE) from None


def _summary(investigation_id: str, bundle: dict, run: dict) -> dict:
    findings = [item for item in bundle["findings"] if item["run_id"] == run["id"]]
    config = run.get("configuration") or {}
    draft_source = config.get("draft_source")
    gaps = run.get("coverage", {}).get("gaps", [])
    return {
        "id": investigation_id,
        "case_id": bundle["case"]["id"],
        "title": bundle["case"]["title"][:300],
        "goal": bundle["case"]["question"][:2000],
        "fingerprint": EvidenceBundle.model_validate(bundle).fingerprint(),
        "run_id": run["id"],
        "draft_source": draft_source[:120] if isinstance(draft_source, str) else "Not recorded",
        "records": len(bundle["records"]),
        "claims": sum(item["details"].get("role") == "claim" for item in findings),
        "steps": sum(item["details"].get("role") == "step" for item in findings),
        "assumptions": sum(item["kind"] == "assumption" for item in findings),
        "gaps": [item[:1000] for item in gaps[:20] if isinstance(item, str)]
        if isinstance(gaps, list)
        else [],
    }


def preview_saved_investigation(service: WorkspaceService, investigation_id: str) -> dict:
    """Read and validate a local CLI bundle without importing it or exposing paths."""
    bundle, run = _load_saved(service, investigation_id)
    summary = _summary(investigation_id, bundle, run)
    try:
        summary["import_status"] = service.repository.preview_import(
            EvidenceBundle.model_validate(bundle)
        )
    except ValueError:
        raise ValueError(
            "This draft conflicts with saved evidence and cannot be imported."
        ) from None
    return summary


def list_saved_investigations(service: WorkspaceService) -> dict:
    """List a bounded local inventory; incomplete and unsafe folders stay unavailable."""
    names = []
    unavailable = 0
    truncated = False
    try:
        with _directory(_root(service), "investigations") as directory:
            with os.scandir(directory) as entries:
                for index, entry in enumerate(entries):
                    if index >= MAX_SCAN_ENTRIES:
                        truncated = True
                        break
                    if entry.name.startswith("."):
                        continue
                    if not entry.is_dir(follow_symlinks=False) or not _FOLDER_ID.fullmatch(
                        entry.name
                    ):
                        unavailable += 1
                        continue
                    names.append(entry.name)
    except FileNotFoundError:
        return {
            "items": [],
            "unavailable": 0,
            "truncated": False,
            "message": "No saved drafts yet.",
        }
    except OSError:
        return {
            "items": [],
            "unavailable": 1,
            "truncated": False,
            "message": "Saved drafts are unavailable in this workspace.",
        }
    items = []
    for name in sorted(names, reverse=True)[:MAX_SAVED_ENTRIES]:
        try:
            items.append(preview_saved_investigation(service, name))
        except ValueError:
            unavailable += 1
    truncated = truncated or len(names) > MAX_SAVED_ENTRIES
    return {"items": items, "unavailable": unavailable, "truncated": truncated, "message": ""}


def _writable(service: WorkspaceService) -> None:
    if service.settings.read_only:
        raise PermissionError("This workspace is read-only; drafts can be inspected but not saved.")


def import_saved_investigation(
    service: WorkspaceService, investigation_id: str, *, expected_fingerprint: str
) -> str:
    """Import only the exact previewed bundle through the native immutable merger."""
    _writable(service)
    bundle, _run = _load_saved(service, investigation_id)
    if (
        not expected_fingerprint
        or EvidenceBundle.model_validate(bundle).fingerprint() != expected_fingerprint
    ):
        raise ValueError("The saved draft changed. Preview it again before importing.")
    return service.import_bundle(bundle)


def _capture_from(service: WorkspaceService, state: Mapping) -> tuple[dict, dict]:
    if not isinstance(state, Mapping) or not state.get("case_id"):
        raise ValueError("Choose a saved case before checking its reasoning.")
    try:
        view = service.view(state["case_id"], state.get("scenario_id"))
        binding = navigation_context(view, state)
        bundle = service.get_case(state["case_id"])
        assessment = assessment_context(view)
        excluded = sorted(view.get("impact", {}).get("excluded_record_ids") or [])
        context = {
            "case_id": bundle["case"]["id"],
            "case_fingerprint": view["case_fingerprint"],
            "run_id": assessment["base_run_id"],
            "scenario_id": state.get("scenario_id"),
            "scenario_version": assessment["version"],
            "excluded_record_ids": excluded,
            "binding": binding,
        }
        if not any(run["id"] == context["run_id"] for run in bundle["runs"]):
            raise ValueError("The selected analysis run is unavailable.")
    except (KeyError, TypeError):
        raise ValueError(_CHANGED) from None
    return bundle, context


def _capture(service: WorkspaceService, state: Mapping) -> tuple[dict, dict]:
    with workspace_snapshot(service) as snapshot:
        return _capture_from(snapshot, state)


def check_context(service: WorkspaceService, state: Mapping) -> dict:
    """Resolve the server's exact current assessment binding without writes."""
    return _capture(service, state)[1]


def draft_context(service: WorkspaceService, state: Mapping) -> dict:
    """List saved text choices from the same read snapshot as the displayed context."""
    bundle, context = _capture(service, state)
    _require_baseline(context)
    _bounded_check(bundle, require_text=False)
    return {
        "context": context,
        "question": bundle["case"]["question"],
        "records": [
            {
                "id": record["id"],
                "label": (
                    record["metadata"]["title"]
                    if isinstance(record.get("metadata", {}).get("title"), str)
                    else record["source_label"]
                )[:300],
                "characters": len(record["text"]),
            }
            for record in bundle["records"]
            if isinstance(record.get("text"), str) and record["text"].strip()
        ],
    }


def _require_baseline(context: dict) -> None:
    if context["excluded_record_ids"]:
        raise ValueError(
            "Reasoning checks are unavailable while this assessment excludes records. "
            "Return to the original evidence or restore the records first; this checker "
            "does not recompute conclusions after exclusions."
        )


def _bounded_check(bundle: dict, *, require_text: bool = True) -> None:
    if require_text and any(not record.get("text") for record in bundle["records"]):
        raise ValueError(
            "Text reasoning checks are unavailable because this case includes records "
            "without saved text. Inspect those records through the existing media or "
            "specialist evidence viewer."
        )
    if require_text and any(
        fragment["selector"].get("kind", "text") != "text" for fragment in bundle["fragments"]
    ):
        raise ValueError(
            "Text reasoning checks are unavailable for whole-record or image selectors. "
            "Use the existing source or specialist evidence viewer for those references."
        )
    if any(len(bundle.get(key, [])) > limit for key, limit in CHECK_LIMITS.items()):
        raise ValueError("This case exceeds the interactive reasoning-check limits. Use the CLI.")
    text_bytes = sum(len((record.get("text") or "").encode()) for record in bundle["records"])
    if text_bytes > MAX_CHECK_TEXT_BYTES or len(json.dumps(bundle).encode()) > MAX_BUNDLE_BYTES:
        raise ValueError(
            "This case exceeds the interactive reasoning-check size limit. Use the CLI."
        )
    # The checker retains a path to each premise. Bound shared-path expansion as
    # well as object counts: a small diamond-shaped DAG can have many paths.
    premises = {finding["snapshot_id"]: [] for finding in bundle["findings"]}
    for link in bundle["links"]:
        if (
            link["relation"] in {"supports", "depends_on"}
            and link["source"]["type"] == "finding"
            and link["target"]["type"] == "finding"
        ):
            premises[link["target"]["id"]].append(link["source"]["id"])
    costs = {}

    def path_cost(finding_id):
        if finding_id not in costs:
            value = 1 + sum(path_cost(source) for source in premises[finding_id])
            if value > MAX_CHECK_TRAVERSALS:
                raise ValueError(
                    "This reasoning graph exceeds the interactive work limit. Use the CLI."
                )
            costs[finding_id] = value
        return costs[finding_id]

    if sum(path_cost(finding_id) for finding_id in premises) > MAX_CHECK_TRAVERSALS:
        raise ValueError("This reasoning graph exceeds the interactive work limit. Use the CLI.")


class _BoundedRecordStore(RecordStore):
    """Reuse frozen-record verification with safe reads and an aggregate byte budget."""

    def __init__(self, root: Path):
        super().__init__(root)
        self._data_root = root
        self._remaining = MAX_FROZEN_TOTAL_BYTES

    def load(self, record_id: str) -> MaltegoLookupRecord:
        self.path(record_id)  # Validate the content-addressed filename first.
        try:
            with _directory(self._data_root, "maltego", "records") as directory:
                content = _read_file(
                    directory, f"{record_id}.json", min(MAX_FROZEN_BYTES, self._remaining)
                )
        except (OSError, ValueError):
            raise FrozenRecordUnavailable(
                "Frozen record verification is unavailable: the file cannot be safely read "
                "within the interactive size and access limits."
            ) from None
        self._remaining -= len(content)
        record = MaltegoLookupRecord.model_validate_json(content)
        if record.record_id != record_id:
            raise ValueError("Frozen record identity mismatch.")
        return record


def check_case(
    service: WorkspaceService, state: Mapping, *, expected_context: dict | None = None
) -> dict:
    """Explicitly check saved evidence, without models, network, jobs, or decisions."""
    bundle, context = _capture(service, state)
    if expected_context is not None and context != expected_context:
        raise ValueError(_CHANGED)
    _require_baseline(context)
    _bounded_check(bundle)
    report = check_bundle(
        bundle,
        store=_BoundedRecordStore(_root(service)),
        reasoner=Reasoner(
            None, not_run_reason="This action runs mechanical and deterministic checks only."
        ),
    ).to_json()
    selected = {
        finding["snapshot_id"]
        for finding in bundle["findings"]
        if finding["run_id"] == context["run_id"]
    }
    report["findings"] = [item for item in report["findings"] if item["id"] in selected]
    report["conclusions"] = [item for item in report["conclusions"] if item["id"] in selected]
    report["links"] = [item for item in report["links"] if item["target"].get("id") in selected]
    chain = [item for item in report["links"] if item["scope"] == "chain"]
    report["summary"].update(
        links={label: sum(item["label"] == label for item in chain) for label in LABELS},
        links_total=len(chain),
        context_links=len(report["links"]) - len(chain),
        conclusions={
            status: sum(item["status"] == status for item in report["conclusions"])
            for status in ("holds", "weak", "undermined")
        },
    )
    report["summary"]["model_checks"]["unchecked_links_a_model_check_could_decide"] = sum(
        item["label"] == "unchecked" for item in chain
    )
    if check_context(service, state) != context:
        raise ValueError(_CHANGED)
    return {
        "context": context,
        "report": report,
        "scope": (
            "Complete saved evidence; conclusions shown only for the selected analysis run. "
            "No exclusions applied."
        ),
    }


class _WriteRepository(WorkspaceRepository):
    def __init__(self, original, connection):
        self._active_connection = connection
        super().__init__(original.db_path)

    @contextmanager
    def _connection(self, *, write=False):
        del write
        yield self._active_connection


@contextmanager
def _write_workspace(service: WorkspaceService):
    with service.repository._connection(write=True) as connection:
        bound = object.__new__(WorkspaceService)
        bound.repository = _WriteRepository(service.repository, connection)
        bound.settings = service.settings
        bound.artifact_root = service.artifact_root
        yield bound


def _text(value: str, label: str, limit: int, *, optional=False) -> str:
    if not isinstance(value, str) or len(value) > limit or (not optional and not value.strip()):
        raise ValueError(f"Supply {label} within {limit:,} characters.")
    return value.strip()


def _session_for_claim(service, case_id, finding, state=None) -> dict:
    from ..review.selectors import source_for_finding

    state = state or {}
    scenario = service.baseline_scenario_state(case_id, finding["snapshot_id"])
    return {
        "case_id": case_id,
        "case_fingerprint": service.case_fingerprint(case_id),
        "finding_id": finding["snapshot_id"],
        "record_id": source_for_finding(service.get_case(case_id), finding["snapshot_id"]),
        "scenario_id": scenario.id,
        "scenario_version": scenario.version,
        "assessment_kind": "baseline",
        "comparison_scenario_id": None,
        "job_id": None,
        "revision": int(state.get("revision") or 0) + 1,
        "navigate_to": "review",
        "navigate_target": "rv-finding-heading",
    }


def open_investigation_session(
    service: WorkspaceService, case_id: str, *, run_id: str | None = None
) -> dict:
    """Explicitly open a requested investigator run, or the latest when unspecified."""
    _writable(service)
    with _write_workspace(service) as current:
        bundle = current.get_case(case_id)
        for run in reversed(bundle["runs"]):
            if run["method"] != INVESTIGATION_METHOD or (run_id and run["id"] != run_id):
                continue
            finding = next(
                (
                    item
                    for item in bundle["findings"]
                    if item["run_id"] == run["id"] and item["details"].get("role") == "claim"
                ),
                None,
            )
            if finding is not None:
                return _session_for_claim(current, case_id, finding)
    raise ValueError("This case or requested run has no investigator claim to open yet.")


def save_manual_draft(
    service: WorkspaceService,
    state: Mapping,
    *,
    claim: str,
    step: str,
    record_id: str,
    quote: str,
    assumption: str = "",
) -> dict:
    """Save and explicitly open a manual proposal over one exact saved passage."""
    _writable(service)
    claim = _text(claim, "a claim", 4000)
    step = _text(step, "a reasoning step", 4000)
    assumption = _text(assumption, "an assumption", 4000, optional=True)
    _text(quote, "an exact quote", 12000)
    bundle, context = _capture(service, state)
    _require_baseline(context)
    _bounded_check(bundle, require_text=False)
    record = next((item for item in bundle["records"] if item["id"] == record_id), None)
    text = record.get("text") if record else None
    if (
        not isinstance(text, str)
        or quote not in text
        or text.find(quote, text.find(quote) + 1) != -1
    ):
        raise ValueError("Choose an exact quote that occurs once in the selected saved record.")
    line = text[: text.index(quote)].count("\n") + 1
    draft = InvestigationDraft.model_validate(
        {
            "claims": [
                {
                    "id": "C1",
                    "statement": claim,
                    "assumption_ids": ["A1"] if assumption else [],
                    "steps": [
                        {
                            "id": "S1",
                            "statement": step,
                            "assumption_ids": [],
                            "citations": [{"record": "R1", "line": line, "quote": quote}],
                        }
                    ],
                }
            ],
            "assumptions": [
                {
                    "id": "A1",
                    "statement": assumption,
                    "reason": "Explicitly stated by the draft author.",
                }
            ]
            if assumption
            else [],
            "gaps": [],
        }
    )
    built = build_saved_investigation_bundle(
        bundle,
        draft,
        goal=bundle["case"]["question"] or claim,
        created_at=datetime.now(UTC).isoformat(),
        record_ids=[record_id],
        configuration={
            "draft_source": "manual",
            "draft_note": "Written in the review desk; a proposal, not a review decision.",
        },
    )
    if built.problems or any(not item.get("accepted") for item in built.citations):
        raise ValueError("The draft's quote could not be bound exactly. Check the saved passage.")
    added = [
        run
        for run in built.bundle["runs"]
        if run["id"] not in {item["id"] for item in bundle["runs"]}
    ]
    if len(added) != 1:
        raise ValueError("The draft did not produce one distinct saved analysis run.")
    run_id = added[0]["id"]
    finding_id = next(
        item["snapshot_id"]
        for item in built.bundle["findings"]
        if item["run_id"] == run_id and item["details"].get("role") == "claim"
    )
    with _write_workspace(service) as current:
        if _capture_from(current, state)[1] != context:
            raise ValueError(_CHANGED)
        case_id = current.import_bundle(built.bundle)
        scenario = current.baseline_scenario_state(case_id, finding_id)
        session = {
            "case_id": case_id,
            "case_fingerprint": current.case_fingerprint(case_id),
            "finding_id": finding_id,
            "record_id": record_id,
            "scenario_id": scenario.id,
            "scenario_version": scenario.version,
            "assessment_kind": "baseline",
            "comparison_scenario_id": None,
            "job_id": None,
            "revision": int(state.get("revision") or 0) + 1,
            "navigate_to": "review",
            "navigate_target": "rv-finding-heading",
        }
    return {"case_id": case_id, "run_id": run_id, "finding_id": finding_id, "session": session}
