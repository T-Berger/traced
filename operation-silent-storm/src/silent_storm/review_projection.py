"""Pure review eligibility and view composition from already-fetched evidence.

The service resolves references and checks live media through ``Store.get_frame``
before calling these functions. Projection does not query storage, inspect files,
or persist a human decision.
"""

from __future__ import annotations

from .contracts import REVIEW_STATUS_LABELS


def eligibility(values: dict, resolved_frames: list[tuple[str, dict | None]], excluded: set[str]):
    """Return eligibility and copied frame views for one saved review revision."""
    reasons, frames = [], []
    if values["status"] != "reviewed":
        reasons.append(
            f"Status is {REVIEW_STATUS_LABELS[values['status']]}; only Reviewed is eligible."
        )
    if not values["observation"]:
        reasons.append("No observation is saved.")
    if not values["rationale"]:
        reasons.append("No review rationale is saved.")
    if not resolved_frames:
        reasons.append("No supporting evidence is saved.")
    for evidence_id, supplied_frame in resolved_frames:
        if supplied_frame is None:
            reasons.append(f"Evidence reference is unavailable: {evidence_id}")
            continue
        frame = {**supplied_frame, "evidence_id": evidence_id}
        frame["source_excluded"] = frame["video_id"] in excluded
        frames.append(frame)
        if not frame["media_available"]:
            reasons.append(f"Evidence media is unavailable: {evidence_id}")
        if frame["source_excluded"]:
            reasons.append(f"Source video is excluded: {frame['video_id']}")
    return {"eligible": not reasons, "reasons": list(dict.fromkeys(reasons))}, frames


def review_view(values: dict, assessment: dict, frames: list[dict], latest_revision: int) -> dict:
    """Attach currentness without mutating the supplied assessment or revision."""
    result = {
        **values,
        "eligibility": {**assessment, "reasons": list(assessment["reasons"])},
        "evidence": frames,
        "is_current": values["revision"] == latest_revision,
    }
    if not result["is_current"]:
        result["eligibility"]["eligible"] = False
        result["eligibility"]["reasons"].append("This is an earlier saved revision.")
    return result
