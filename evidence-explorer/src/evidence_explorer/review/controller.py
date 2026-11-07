"""Review state transitions shared by Dash callbacks and workflow tests.

Only identifiers and view versions are kept in the browser. Services own evidence
and durable decisions; selecting a case or an assessment never writes to storage.
"""

from __future__ import annotations

from typing import Any

from .selectors import bundle_data, evidence_ids, finding_id, initial_selection, source_for_finding
from .service import ReviewService
from .storage import VersionConflictError


def session_for_case(service: ReviewService, case_id: str | None) -> dict[str, Any]:
    view = service.view(case_id) if case_id else {}
    selected, record = initial_selection(bundle_data(view))
    return {
        "case_id": case_id,
        "case_fingerprint": view.get("case_fingerprint"),
        "finding_id": selected,
        "record_id": record,
        "scenario_id": None,
        "scenario_version": None,
        "comparison_scenario_id": None,
        "job_id": None,
        "revision": 0,
    }


def select_finding(service: ReviewService, state: dict, selected: str) -> None:
    view = service.view(state["case_id"], state.get("scenario_id"))
    # A graph can lead to a separately saved run. Verify it against the full case.
    bundle = service.get_case(state["case_id"])
    finding = next((f for f in bundle["findings"] if finding_id(f) == selected), None)
    if finding is None:
        raise ValueError("This finding is no longer available. Choose another finding.")
    scenario = view.get("scenario")
    if scenario and scenario["base_run_id"] != finding["run_id"]:
        state.update(scenario_id=None, scenario_version=None)
    state.update(
        finding_id=selected,
        record_id=source_for_finding(bundle, selected),
        case_fingerprint=view.get("case_fingerprint"),
        job_id=None,
    )


def select_assessment(service: ReviewService, state: dict, selected: str | None) -> None:
    scenario_id = None if selected in {None, "baseline"} else selected
    view = service.view(state["case_id"], scenario_id)
    bundle = bundle_data(view)
    current = state.get("finding_id")
    if current not in {finding_id(f) for f in bundle.get("findings", [])}:
        current, record = initial_selection(bundle)
    else:
        record = state.get("record_id")
        linked, _ = evidence_ids(bundle, current)
        if record not in linked:
            record = source_for_finding(bundle, current)
    scenario = view.get("scenario") or {}
    if state.get("scenario_id") and not view.get("is_baseline", False):
        state["comparison_scenario_id"] = scenario_id
    state.update(
        scenario_id=scenario_id,
        scenario_version=scenario.get("version"),
        finding_id=current,
        record_id=record,
        case_fingerprint=view.get("case_fingerprint"),
        job_id=None,
    )


def ensure_assessment(service: ReviewService, state: dict) -> None:
    if state.get("scenario_id"):
        return
    scenario = service.baseline_scenario(state["case_id"], state.get("finding_id"))
    state.update(scenario_id=scenario["id"], scenario_version=scenario["version"])


def mutate_review(
    service: ReviewService,
    state: dict,
    action: str,
    *,
    exclude_reason: str = "",
    review_reason: str = "",
    challenge_mode: str = "demo",
) -> str:
    if not state.get("case_id") or not state.get("finding_id"):
        raise ValueError("Choose a case and a finding first.")
    current_fingerprint = service.case_fingerprint(state["case_id"])
    if state.get("case_fingerprint") != current_fingerprint:
        raise VersionConflictError("This case changed. Review the latest evidence before saving.")
    if action == "rv-toggle-source":
        if not state.get("record_id"):
            raise ValueError("Open a supporting record first.")
        if not str(exclude_reason or "").strip():
            raise ValueError("Add a short reason before excluding or restoring a record.")
        view = service.view(state["case_id"], state.get("scenario_id"))
        # Never mutate the durable baseline merely because it has a storage ID.
        if not state.get("scenario_id") or view.get("is_baseline") or view.get("imported"):
            scenario = service.start_scenario(state["case_id"], state["finding_id"])
            state.update(scenario_id=scenario["id"], scenario_version=scenario["version"])
            view = service.view(state["case_id"], scenario["id"])
        excluded = state["record_id"] not in view["impact"]["excluded_record_ids"]
        scenario = service.exclude(
            state["scenario_id"],
            state["record_id"],
            excluded,
            exclude_reason.strip(),
            state["scenario_version"],
        )
        state.update(
            scenario_version=scenario["version"],
            comparison_scenario_id=scenario["id"],
            job_id=None,
            revision=state.get("revision", 0) + 1,
        )
        return (
            "Record excluded. Review the affected findings; the baseline is preserved."
            if excluded
            else "Record restored. Earlier decisions stay in history; review this version."
        )
    if action in {"rv-use", "rv-qualify", "rv-withhold"} and not str(review_reason or "").strip():
        raise ValueError("Add your reason or qualified wording before saving a decision.")
    ensure_assessment(service, state)
    if action == "rv-challenge":
        job = service.challenge(
            state["case_id"],
            state["finding_id"],
            state["scenario_id"],
            mode=challenge_mode or "demo",
        )
        state["job_id"] = job["id"]
        return "Evidence check queued. Its progress and citations appear in the check section."
    dispositions = {"rv-use": "use", "rv-qualify": "qualify", "rv-withhold": "withhold"}
    if action in dispositions:
        if not str(review_reason or "").strip():
            raise ValueError("Add your reason or qualified wording before saving a decision.")
        service.review(
            state["case_id"],
            state["finding_id"],
            state["scenario_id"],
            dispositions[action],
            review_reason.strip(),
            expected_version=state.get("scenario_version"),
            expected_fingerprint=state.get("case_fingerprint"),
        )
        state["revision"] = state.get("revision", 0) + 1
        return "Decision saved for this finding and assessment. Preview it in Brief."
    raise ValueError("This review action is unavailable.")
