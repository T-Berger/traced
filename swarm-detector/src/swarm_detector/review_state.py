"""Case/run-scoped draft and saved-revision rules for the analyst interface."""

from __future__ import annotations

from copy import deepcopy

FIELD_IDS = (
    "reviewer-name",
    "review-decision",
    "review-reason",
    "narrative-claim",
    "narrative-endorsement",
    "narrative-alignment",
    "narrative-notes",
    "supporting-evidence",
)
FIELDS = (
    "reviewer",
    "decision",
    "reason",
    "claim",
    "endorsement",
    "alignment",
    "narrative_notes",
    "evidence_refs",
)


def values_from_review(review: dict | None = None) -> dict:
    review = review or {}
    return {
        "reviewer": review.get("reviewer") or "",
        "decision": review.get("decision") or "Insufficient evidence",
        "reason": review.get("reason") or "",
        "claim": review.get("claim") or "",
        "endorsement": review.get("endorsement") or "unclear",
        "alignment": review.get("alignment") or "unclear",
        "narrative_notes": review.get("narrative_notes") or "",
        "evidence_refs": sorted(set(review.get("evidence_refs") or [])),
    }


def form_values(values: list | tuple) -> dict:
    return values_from_review(dict(zip(FIELDS, values, strict=True)))


def dirty(values: dict, state: dict | None) -> bool:
    return bool(state and values_from_review(values) != values_from_review(state.get("baseline")))


def preserve_draft(drafts: dict | None, state: dict | None, values: dict) -> dict:
    result = deepcopy(drafts or {})
    if not state or not state.get("key"):
        return result
    if dirty(values, state):
        result[state["key"]] = {
            "values": values_from_review(values),
            "baseline": values_from_review(state.get("baseline")),
            "revision": int(state.get("revision") or 0),
        }
    else:
        result.pop(state["key"], None)
    return result


def open_review(
    detail: dict, drafts: dict | None, old_state: dict | None = None, old_values: dict | None = None
) -> tuple[dict, dict, dict]:
    """Capture the outgoing form before restoring another case's draft."""
    drafts = preserve_draft(drafts, old_state, old_values or values_from_review())
    case_id = (detail.get("case") or {}).get("case_id")
    run_id = (detail.get("run") or {}).get("run_id")
    if not case_id or not run_id:
        return {}, values_from_review(), drafts
    key = f"{run_id}::{case_id}"
    saved = detail.get("review") or {}
    revision = int(saved.get("revision") or 0)
    prior = drafts.get(key)
    state = {
        "key": key,
        "case_id": case_id,
        "run_id": run_id,
        "baseline": values_from_review(saved),
        "revision": revision,
        "latest_revision": revision,
    }
    if prior:
        state.update(
            baseline=values_from_review(prior.get("baseline")),
            revision=int(prior.get("revision") or 0),
        )
        return state, values_from_review(prior.get("values")), drafts
    return state, values_from_review(saved), drafts


def validate_review(values: dict) -> str | None:
    if not str(values.get("reviewer") or "").strip():
        return "Add a reviewer name or analyst ID."
    if len(str(values.get("reason") or "").strip()) < 10:
        return "Explain the evidence and alternative explanations in your review reason."
    if values.get("alignment") == "supported" and not str(values.get("claim") or "").strip():
        return "Define the claim before recording supported narrative alignment."
    if str(values.get("claim") or "").strip() or values.get("alignment") == "supported":
        if not values.get("evidence_refs") or not str(values.get("narrative_notes") or "").strip():
            return "A narrative claim needs supporting source comments and contextual reasoning."
    return None


def save_review(wb, state: dict, values: dict, drafts: dict | None) -> tuple[dict, dict, dict]:
    """Save only the form's pinned case/run and expected revision."""
    if not state.get("case_id") or not state.get("run_id"):
        raise ValueError("Select a case with a saved analysis before reviewing.")
    problem = validate_review(values)
    if problem:
        raise ValueError(problem)
    saved = wb.save_review(
        state["case_id"],
        **values_from_review(values),
        run_id=state["run_id"],
        expected_revision=int(state.get("revision") or 0),
    )
    values = values_from_review(saved)
    updated = {
        **state,
        "baseline": values,
        "revision": int(saved.get("revision") or 0),
        "latest_revision": int(saved.get("revision") or 0),
    }
    drafts = deepcopy(drafts or {})
    drafts.pop(state["key"], None)
    return updated, values, drafts


def export_preview(wb, preview: dict, state: dict, values: dict, export_format: str) -> str:
    """Download exactly the saved snapshot the analyst has previewed."""
    if not preview or not state or preview.get("key") != state.get("key"):
        raise ValueError("Preview the saved brief for this case before exporting.")
    if dirty(values, state):
        raise ValueError("Save your changes or explicitly discard them before exporting.")
    if preview.get("revision") != state.get("revision"):
        raise ValueError("The saved review changed. Preview the current saved revision first.")
    return wb.export_case(
        state["case_id"],
        format=export_format,
        run_id=state["run_id"],
        revision=int(preview.get("revision") or 0),
    )
