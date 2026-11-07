"""Read-only, context-bound next-action navigation for the existing review pages.

Recommendations describe an available action, never workflow completion. Dispatch
loads the current assessment again and verifies every target before returning a
new browser session. It creates no scope, collection, scenario, decision, or file.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from .drafts import draft_key
from .selectors import evidence_ids, source_for_finding
from .workflow import assessment_context, assessment_decisions

_ROUTES = {
    "purpose": ("cases", "trail-planning-question"),
    "saved_plan": ("cases", "trail-planning-picker"),
    "scope": ("cases", "trail-planning-scope"),
    "plan_history": ("cases", "trail-planning-history"),
    "plan_continue": ("cases", "trail-planning-resume"),
    "plan_save": ("cases", "trail-planning-save"),
    "plan_attach": ("cases", "trail-planning-bind"),
    "plan_confirm": ("cases", "trail-planning-confirm"),
    "intake": ("cases", "trail-gather-evidence"),
    "finding": ("review", "rv-finding-heading"),
    "source": ("review", "rv-source"),
    "compare": ("compare", "rv-source-heading"),
    "review": ("review", "rv-review-reason"),
    "brief": ("brief", "rv-brief-page"),
}


class NavigationContextError(ValueError):
    """The requested navigation no longer matches the visible saved assessment."""


@dataclass(frozen=True)
class NextAction:
    kind: str
    label: str
    explanation: str
    context: str
    finding_id: str = ""
    record_id: str = ""
    location: str = "guidance"

    def button_id(self) -> dict[str, str]:
        return {
            "type": "rv-next-action",
            "action": self.kind,
            "context": self.context,
            "fid": self.finding_id,
            "rid": self.record_id,
            "location": self.location,
        }


def _binding(view: Mapping[str, Any]) -> str:
    bundle = view.get("bundle", {})
    context = assessment_context(view)
    scope = [
        bundle.get("case", {}).get("id"),
        view.get("case_fingerprint"),
        "baseline" if context["is_baseline"] else context["id"],
        context["version"],
        context["base_run_id"],
    ]
    encoded = json.dumps(scope, ensure_ascii=False, separators=(",", ":"))
    return "nav-v1:" + hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def navigation_context(view: Mapping[str, Any], state: Mapping[str, Any]) -> str:
    """Bind another read-only link to the same validated case and assessment."""
    _validate_state(view, state)
    return _binding(view)


def _active_findings(view: Mapping[str, Any]) -> dict[str, dict]:
    run_id = assessment_context(view)["base_run_id"]
    return {
        finding["snapshot_id"]: finding
        for finding in view.get("bundle", {}).get("findings", [])
        if finding.get("run_id") == run_id
    }


def _state_for_view(view: Mapping[str, Any], state: Mapping[str, Any]) -> dict:
    context = assessment_context(view)
    return {
        **state,
        "case_id": view.get("bundle", {}).get("case", {}).get("id"),
        "case_fingerprint": view.get("case_fingerprint"),
        "scenario_id": context["id"],
        "scenario_version": context["version"],
        "assessment_kind": "baseline" if context["is_baseline"] else "scenario",
    }


def _source(view: Mapping[str, Any], finding_id: str, preferred: Any = None) -> str:
    bundle = view.get("bundle", {})
    linked, _ = evidence_ids(bundle, finding_id)
    records = {record["id"] for record in bundle.get("records", [])}
    if isinstance(preferred, str) and preferred in linked and preferred in records:
        return preferred
    return source_for_finding(bundle, finding_id) or ""


def _dirty_finding(
    view: Mapping[str, Any], state: Mapping[str, Any], drafts: Mapping | None, note: str | None
) -> str:
    findings = _active_findings(view)
    current = assessment_decisions(view)
    active_id = state.get("finding_id")
    active = active_id if active_id in findings else ""
    if active and note is not None and note != current.get(active, {}).get("reason", ""):
        return active
    notes = drafts.get("notes", {}) if isinstance(drafts, Mapping) else {}
    if not isinstance(notes, Mapping):
        return ""
    scoped_state = _state_for_view(view, state)
    candidates = [active, *[fid for fid in findings if fid != active]]
    for finding_id in candidates:
        if not finding_id:
            continue
        key = draft_key({**scoped_state, "finding_id": finding_id}, dict(view))
        text = notes.get(key)
        if isinstance(text, str) and text != current.get(finding_id, {}).get("reason", ""):
            return finding_id
    return ""


def _step_status(progress: Any, step_id: str) -> str:
    steps = (
        progress.get("steps", ())
        if isinstance(progress, Mapping)
        else getattr(progress, "steps", ())
    )
    for step in steps:
        key = step.get("id") if isinstance(step, Mapping) else getattr(step, "id", None)
        if key == step_id:
            status = (
                step.get("status") if isinstance(step, Mapping) else getattr(step, "status", "")
            )
            return str(status)
    return ""


def contextual_next_action(
    view: Mapping[str, Any],
    state: Mapping[str, Any],
    *,
    page: str = "cases",
    purpose_id: str | None = None,
    progress: Any = None,
    drafts: Mapping | None = None,
    note: str | None = None,
    planning_stage: str | None = None,
) -> NextAction:
    """Choose one existing destination without changing persisted workflow facts."""
    view, state = view or {}, state or {}
    context = navigation_context(view, state)
    bundle = view.get("bundle", {})
    records = bundle.get("records", [])
    scope_pending = _step_status(progress, "scope") not in {"", "completed"} or bool(
        records and planning_stage and planning_stage != "confirmed"
    )
    scope_note = " Public-source scope still needs confirmation." if scope_pending else ""

    def action(kind, label, explanation, finding_id="", record_id=""):
        return NextAction(kind, label, explanation + scope_note, context, finding_id, record_id)

    dirty = _dirty_finding(view, state, drafts, note)
    if dirty:
        return action(
            "review",
            "Continue this review",
            "Your unsaved note is kept with this finding. Finish or revise it here.",
            dirty,
            _source(view, dirty, state.get("record_id")),
        )
    if planning_stage and (
        page == "cases"
        or not records
        or planning_stage
        in {"attach", "scope", "confirm", "confirm_saved", "history", "continue", "attach_ready"}
    ):
        if planning_stage == "history":
            return action(
                "plan_history",
                "Review saved plan version",
                "Adopt this version locally or copy its fields into a separate plan "
                "before continuing.",
            )
        if planning_stage in {"start", "draft"}:
            return action(
                "plan_save" if planning_stage == "draft" else "purpose",
                "Continue investigation plan"
                if planning_stage == "draft"
                else "Start investigation",
                "Your plan has unsaved changes. Review the fields and Save plan before continuing."
                if planning_stage == "draft"
                else (
                    "Write your question and save a plan. "
                    "Then gather evidence and confirm its scope."
                ),
            )
        if planning_stage == "continue":
            return action(
                "plan_continue",
                "Review gathered evidence for your plan",
                "Your saved plan is ready to continue. Check the selected case, then choose "
                "Use this case in saved plan. Nothing is attached automatically.",
            )
        if planning_stage == "saved":
            return action(
                "intake",
                "Gather evidence",
                "Your plan is saved. Collect or import evidence, then continue the same plan "
                "with the saved case. You can also use an existing case's sources in the plan.",
            )
        if planning_stage in {"resume", "attach", "confirm_saved"}:
            return action(
                "saved_plan",
                "Attach investigation plan" if planning_stage == "attach" else "Open saved plan",
                "Open the saved plan you want to use. Your current working fields are kept.",
            )
        if planning_stage == "scope":
            return action(
                "scope",
                "Review evidence scope",
                "Review this case's sources, then use the plan controls to save and attach them.",
            )
        if planning_stage == "confirm":
            return action(
                "plan_confirm",
                "Confirm investigation scope",
                "Check the attached plan and saved evidence, then select Confirm scope.",
            )
        if planning_stage == "attach_ready":
            return action(
                "plan_attach",
                "Attach evidence to your plan",
                "The saved source selection matches this case. Select Attach collected evidence "
                "to associate this exact case, then confirm its scope.",
            )
    if not purpose_id and not records:
        return action(
            "purpose", "Start investigation", "Write your question and save an investigation plan."
        )
    if not bundle.get("case", {}).get("id") or not records:
        return action(
            "intake",
            "Collect or import evidence",
            "Open the existing intake controls to save public evidence.",
        )
    findings = _active_findings(view)
    decided = assessment_decisions(view)
    if findings and all(finding_id in decided for finding_id in findings):
        return action(
            "brief",
            "Preview reviewed briefing",
            "Current decisions are saved. Preview wording and omissions before exporting.",
        )
    if planning_stage == "confirmed" and page == "cases" and findings:
        first = next((fid for fid in findings if fid not in decided), next(iter(findings)))
        collection_only = findings[first].get("details", {}).get("domain") == "sources"
        return action(
            "finding",
            "Inspect collected material" if collection_only else "Open first finding",
            (
                "Scope confirmed. These findings describe collection counts or repeated text. "
                "Inspect the material before requesting or importing substantive analysis."
                if collection_only
                else "Scope confirmed. Open the first finding awaiting your decision and inspect "
                "its exact evidence. Earlier review decisions stay unchanged."
            ),
            first,
            _source(view, first),
        )
    current = state.get("finding_id")
    current = current if current in findings else next(iter(findings), "")
    if not findings:
        text_records = [record for record in records if record.get("text")]
        record_id = state.get("record_id")
        if record_id not in {record["id"] for record in records}:
            record_id = records[0]["id"]
        if len(text_records) >= 2:
            return action(
                "compare",
                "Compare saved sources",
                "This run has no findings. Compare sources without inferring independence.",
                record_id=record_id,
            )
        return action(
            "source",
            "Inspect saved evidence",
            "Inspect the supplied record and provenance; this creates no finding or review.",
            record_id=record_id,
        )
    source_id = _source(view, current, state.get("record_id"))
    if (
        page == "cases"
        and source_id
        and findings[current].get("details", {}).get("domain") == "sources"
    ):
        return action(
            "source",
            "Inspect collected material",
            "These findings describe collection counts or repeated text. Inspect the material, "
            "then use article analysis or import a claim or specialist assessment to investigate "
            "what it supports.",
            current,
            source_id,
        )
    if page == "cases" and source_id:
        return action(
            "source",
            "Inspect exact evidence",
            "Open the saved source behind this finding before deciding what its wording supports.",
            current,
            source_id,
        )
    undecided = next((finding_id for finding_id in findings if finding_id not in decided), current)
    target = current if current not in decided else undecided
    target_source = _source(view, target, state.get("record_id") if target == current else None)
    if not target_source:
        return action(
            "review",
            "Review unsupported wording",
            "No saved source is linked. Review that limitation; missing support is not approval.",
            target,
        )
    if (
        page != "compare"
        and _step_status(progress, "signals") == "unavailable"
        and sum(bool(record.get("text")) for record in records) >= 2
    ):
        return action(
            "compare",
            "Compare saved source context",
            "Signal evaluation is unavailable. Compare sources without completing that evaluation.",
            target,
            target_source,
        )
    return action(
        "review",
        "Review this finding" if target == current else "Review the next finding",
        "Inspect its exact evidence and record Use, Qualify, or Withhold for this assessment.",
        target,
        target_source,
    )


def _validate_state(view: Mapping[str, Any], state: Mapping[str, Any]) -> None:
    if not isinstance(view, Mapping) or not isinstance(state, Mapping):
        raise NavigationContextError("The case selection changed. Choose the next action again.")
    bundle = view.get("bundle", {})
    case_id = state.get("case_id")
    if not case_id:
        if bundle.get("case", {}).get("id"):
            raise NavigationContextError(
                "The case selection changed. Choose the next action again."
            )
        if (
            state.get("case_fingerprint")
            or state.get("scenario_id")
            or state.get("finding_id")
            or state.get("record_id")
        ):
            raise NavigationContextError(
                "The case selection changed. Choose the next action again."
            )
        return
    if (
        bundle.get("case", {}).get("id") != case_id
        or not view.get("case_fingerprint")
        or state.get("case_fingerprint") != view.get("case_fingerprint")
    ):
        raise NavigationContextError(
            "The saved evidence changed. Refresh the case before navigating."
        )
    context = assessment_context(view)
    selected = view.get("scenario") or {}
    if state.get("scenario_id") and selected.get("id") != state["scenario_id"]:
        raise NavigationContextError(
            "The selected assessment changed. Choose the next action again."
        )
    if not state.get("scenario_id") and selected and not context["is_baseline"]:
        raise NavigationContextError(
            "The baseline selection changed. Choose the next action again."
        )
    version = state.get("scenario_version") or 1
    if type(version) is not int or version != context["version"]:
        raise NavigationContextError(
            "The assessment version changed. Choose the next action again."
        )
    for key in ("base_run_id", "run_id"):
        if state.get(key) is not None and state[key] != context["base_run_id"]:
            raise NavigationContextError("The analysis run changed. Choose the next action again.")
    if state.get("finding_id") and state["finding_id"] not in _active_findings(view):
        raise NavigationContextError("The selected finding is outside this assessment's run.")
    if state.get("record_id") and state["record_id"] not in {
        record["id"] for record in bundle.get("records", [])
    }:
        raise NavigationContextError("The selected evidence no longer belongs to this case.")


def apply_navigation(
    service: Any,
    state: Mapping[str, Any],
    action_id: Mapping[str, Any],
    *,
    note: str | None = None,
) -> dict[str, Any]:
    """Validate a click against fresh saved evidence, then return a new session."""
    state = state or {}
    if not isinstance(state, Mapping):
        raise NavigationContextError("The case selection changed. Choose the next action again.")
    if not isinstance(action_id, Mapping) or set(action_id) != {
        "type",
        "action",
        "context",
        "fid",
        "rid",
        "location",
    }:
        raise NavigationContextError("This navigation action is unavailable.")
    if (
        action_id.get("type") != "rv-next-action"
        or not all(
            isinstance(action_id.get(key), str)
            for key in ("action", "context", "fid", "rid", "location")
        )
        or action_id.get("action") not in _ROUTES
        or not action_id["location"].strip()
        or len(action_id["location"]) > 200
    ):
        raise NavigationContextError("This navigation action is unavailable.")
    try:
        view = (
            service.view(state["case_id"], state.get("scenario_id")) if state.get("case_id") else {}
        )
    except (KeyError, ValueError) as exc:
        raise NavigationContextError(
            "This case or assessment changed. Choose it again before navigating."
        ) from exc
    _validate_state(view, state)
    if action_id["context"] != _binding(view):
        raise NavigationContextError(
            "This action belongs to an earlier case or assessment. Choose the next action again."
        )
    kind, finding_id, record_id = action_id["action"], action_id["fid"], action_id["rid"]
    findings = _active_findings(view)
    records = {record["id"] for record in view.get("bundle", {}).get("records", [])}
    if finding_id and finding_id not in findings:
        raise NavigationContextError("The target finding is outside the current assessment's run.")
    if kind == "source" and finding_id and not record_id:
        record_id = _source(view, finding_id)
    if record_id and record_id not in records:
        raise NavigationContextError("The target source no longer belongs to this case.")
    if (
        kind in {"source", "finding"}
        and finding_id
        and record_id
        and record_id not in evidence_ids(view["bundle"], finding_id)[0]
    ):
        raise NavigationContextError("That source is not declared evidence for this finding.")
    if kind == "source" and not record_id:
        raise NavigationContextError("Choose an available saved source before inspecting evidence.")
    if not state.get("case_id") and (
        kind not in {"purpose", "saved_plan", "plan_history", "plan_save", "intake"}
        or finding_id
        or record_id
    ):
        raise NavigationContextError("Choose a case before opening evidence or a review.")
    if kind in {"review", "brief", "finding"} and not findings:
        raise NavigationContextError("This assessment has no findings to review or brief.")
    if kind in {"review", "finding"} and not finding_id:
        raise NavigationContextError("Choose a current finding before opening its review.")
    # A keypress can arrive after the recommendation was rendered. Never leave
    # that active note behind merely because the old button pointed elsewhere.
    dirty = _dirty_finding(view, state, None, note)
    if dirty:
        kind, finding_id = "review", dirty
        record_id = state.get("record_id") or _source(view, dirty)
    result = dict(state)
    for key in (
        "navigate_to",
        "navigate_target",
        "graph_edge_id",
        "focus_fragment_ids",
        "graph_edge",
        "focus_fragment",
    ):
        result.pop(key, None)
    if finding_id:
        if finding_id != result.get("finding_id"):
            result["job_id"] = None
        result["finding_id"] = finding_id
        if not record_id:
            record_id = _source(view, finding_id)
        result["record_id"] = record_id or None
    elif record_id:
        result["record_id"] = record_id
    result["navigate_to"], result["navigate_target"] = _ROUTES[kind]
    return result
