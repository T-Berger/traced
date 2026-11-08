"""Read-only handoff operations over existing review, artifact, and export services."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass

from ..contracts import EvidenceBundle
from ..drafts import unsaved_for_export
from ..navigation import navigation_context
from ..workflow import brief_projection
from .briefs import render_brief
from .handoff import project_handoff, project_import_receipt
from .packages import export_case_package
from .snapshots import workspace_snapshot

_UNSET = object()


def _planning_dirty(state, planning_draft):
    return (
        isinstance(planning_draft, Mapping)
        and bool(state.get("case_id"))
        and planning_draft.get("case_id") == state["case_id"]
        and planning_draft.get("dirty") is True
    )


def _download_context(view, binding, captured):
    # Navigation identifies the route, not its saved decisions. Bind the actual
    # displayed review state too, including edits that leave that route unchanged.
    assessment = json.dumps([binding, view], sort_keys=True, separators=(",", ":"))
    return {
        "assessment_binding": "handoff-v1:" + hashlib.sha256(assessment.encode()).hexdigest(),
        "planning_context_token": captured.planning_context_token,
    }


def validate_download_context(current, expected=_UNSET):
    """Browser downloads require the exact context shown by the handoff preview."""
    if expected is _UNSET:
        return
    if not isinstance(expected, dict) or expected != current:
        raise ValueError("The handoff changed or is not ready. Refresh handoff before downloading.")


@dataclass(frozen=True)
class PreparedExport:
    filename: str
    content: bytes | str
    binding: str
    message: str


def check_artifact_availability(service, bundle) -> dict[str, bool]:
    """Check referenced local media through the existing path/hash validator."""
    saved = bundle if isinstance(bundle, EvidenceBundle) else EvidenceBundle.model_validate(bundle)
    availability = {}
    for record in saved.records:
        if not record.artifact_ref:
            continue
        try:
            service.artifact(saved.case.id, record.id, bundle=saved)
        except (ValueError, KeyError, OSError):
            availability[record.id] = False
        else:
            availability[record.id] = True
    return availability


def load_handoff_context(
    service,
    state,
    drafts=None,
    note=None,
    purpose_id=None,
    *,
    planning_draft=None,
    category_selection=None,
):
    """Load the current assessment, preserving the existing draft/version guards."""
    state = state or {}
    if not state.get("case_id"):
        raise ValueError("Choose a case before preparing a handoff.")
    with workspace_snapshot(service) as snapshot:
        view = snapshot.view(state["case_id"], state.get("scenario_id"))
        binding = navigation_context(view, state)
        captured = snapshot.capture_case_package(state["case_id"])
        category = None
        if category_selection is not None:
            from ...compliance_review.swarm_context import project_category_swarm_context

            if not isinstance(category_selection, Mapping):
                raise ValueError("Choose a category scope in Compare before preparing the dossier.")
            category = project_category_swarm_context(
                view,
                captured.bundle,
                target=category_selection.get("target"),
                record_kinds=category_selection.get("record_kinds"),
                swarm_run_ids=category_selection.get("swarm_run_ids"),
            )
    planning_dirty = _planning_dirty(state, planning_draft)
    dirty = unsaved_for_export(state, view, drafts, note) or planning_dirty
    # Typing a draft must not rehash all media on every keystroke. A clean view
    # checks availability again, and package creation always revalidates files.
    availability = None if dirty else check_artifact_availability(service, captured.bundle)
    handoff = project_handoff(
        view,
        purpose_id=purpose_id,
        dirty=dirty,
        artifact_availability=availability,
    )
    from .planning_handoff import planning_summary

    handoff["planning"] = planning_summary(captured)
    handoff["planning_dirty"] = planning_dirty
    handoff["download_context"] = _download_context(view, binding, captured)
    if category is not None:
        # Bind both the explicit selection and the entire detached projection to
        # the preview, including review changes not reflected in a case hash.
        payload = json.dumps([category_selection, category], sort_keys=True, separators=(",", ":"))
        handoff["download_context"]["category_context"] = (
            "category-handoff-v1:" + hashlib.sha256(payload.encode()).hexdigest()
        )
        handoff["category_review"] = category
    return view, handoff, binding


def import_receipt_for(service, case_id):
    """Describe the validated saved case without treating import as acceptance."""
    with workspace_snapshot(service) as snapshot:
        captured = snapshot.capture_case_package(case_id)
    bundle = captured.bundle
    receipt = project_import_receipt(
        bundle, artifact_availability=check_artifact_availability(service, bundle)
    )
    from .planning_handoff import planning_summary

    receipt["planning"] = planning_summary(captured)
    return receipt


def prepare_export(
    service,
    kind,
    state,
    drafts=None,
    note=None,
    *,
    context=None,
    expected_context=_UNSET,
    planning_draft=None,
) -> PreparedExport:
    """Validate and capture one read snapshot, then serialize only its saved data."""
    state = state or {}
    if not state.get("case_id"):
        raise ValueError("Choose a case before preparing a handoff.")
    if kind not in {"package", "brief", "json"}:
        raise ValueError("Choose a supported export format.")
    if _planning_dirty(state, planning_draft):
        raise ValueError(
            "Unsaved planning changes. Save or discard the planning draft before downloading."
        )
    case_id = state["case_id"]
    with workspace_snapshot(service) as snapshot:
        view = snapshot.view(case_id, state.get("scenario_id"))
        binding = navigation_context(view, state)
        if unsaved_for_export(state, view, drafts, note):
            raise ValueError("Unsaved changes. Save your decision in Review before downloading.")
        captured = snapshot.capture_case_package(case_id)
        download_context = _download_context(view, binding, captured)
        validate_download_context(download_context, expected_context)
        # A review event can change without changing the case fingerprint or
        # scenario version. Compare the full validated view, not only its route.
        if context is not None and (context[2] != binding or context[0] != view):
            raise ValueError("The saved assessment changed. Refresh it before downloading.")
        if context is not None:
            validate_download_context(download_context, context[1].get("download_context"))
        bundle = captured.bundle
    # No live repository reads after this point. Package creation verifies the
    # actual media bytes it retains against the captured bundle's content hashes.
    if kind == "package":
        content = export_case_package(
            bundle, service.artifact_root, planning_history=captured.planning_history
        )
        filename = "evidence-trail-case.zip"
        message = (
            "Case package prepared with saved evidence, reviews, and relevant planning history."
        )
    elif kind == "brief":
        content = render_brief(brief_projection(view))
        filename = "evidence-trail-brief.md"
        message = "Brief prepared for download from the selected saved assessment."
    elif kind == "json":
        content = bundle.model_dump_json(indent=2)
        filename = "evidence-trail-case.json"
        message = (
            "Evidence JSON prepared with evidence and review history only. Investigation briefs, "
            "planning history, and local media are omitted; "
            "use the case package for the full handoff."
        )
    return PreparedExport(filename, content, binding, message)


def prepare_dossier(
    service,
    state,
    drafts=None,
    note=None,
    purpose_id=None,
    *,
    expected_context=_UNSET,
    planning_draft=None,
    category_selection=None,
) -> PreparedExport:
    """Render a draft from one captured evidence and planning snapshot."""
    from .handoff import render_dossier
    from .planning_handoff import planning_dossier_section

    _, handoff, binding = load_handoff_context(
        service,
        state,
        drafts,
        note,
        purpose_id,
        planning_draft=planning_draft,
        category_selection=category_selection,
    )
    if handoff["planning_dirty"]:
        raise ValueError(
            "Unsaved planning changes. Save or discard the planning draft before downloading."
        )
    if handoff["dirty"]:
        raise ValueError("Unsaved changes. Save your decision in Review before downloading.")
    validate_download_context(handoff["download_context"], expected_context)
    if handoff.get("category_review") is not None:
        from ...compliance_review.dossier import redact_category_brief, render_category_dossier

        handoff = redact_category_brief(handoff)
    content = render_dossier(handoff) + "\n\n" + planning_dossier_section(handoff["planning"])
    if handoff.get("category_review") is not None:
        content += "\n\n" + render_category_dossier(handoff["category_review"])
    return PreparedExport(
        "traced-draft-dossier.md",
        content,
        binding,
        "Draft dossier prepared for download. No approval, acceptance, or submission was recorded.",
    )
