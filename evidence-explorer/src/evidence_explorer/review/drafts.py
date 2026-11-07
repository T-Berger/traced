"""Browser draft identity and export checks; draft text never enters the brief."""

from __future__ import annotations

import json

from .workflow import assessment_decisions


def draft_key(state: dict, view: dict | None = None) -> str:
    # Baseline has a durable context only after the first save. Keep its draft
    # identity stable across that transition with a separate assessment label.
    baseline = (view or {}).get(
        "is_baseline", state.get("assessment_kind") == "baseline" or not state.get("scenario_id")
    )
    return json.dumps(
        [
            state.get("case_id"),
            state.get("finding_id"),
            "baseline" if baseline else state.get("scenario_id"),
            state.get("scenario_version") or 1,
            state.get("case_fingerprint"),
        ],
        separators=(",", ":"),
    )


def unsaved_for_export(
    state: dict, view: dict, drafts: dict | None, note: str | None = None
) -> bool:
    current = assessment_decisions(view)
    saved_note = current.get(state.get("finding_id"), {}).get("reason", "")
    if note is not None and note != saved_note:
        return True
    context = draft_key(state, view)
    for key, text in (drafts or {}).get("notes", {}).items():
        try:
            case_id, fid, assessment, version, fingerprint = json.loads(key)
            active_case, _, active_assessment, active_version, active_fingerprint = json.loads(
                context
            )
        except (ValueError, TypeError):
            continue
        if (case_id, assessment, version, fingerprint) == (
            active_case,
            active_assessment,
            active_version,
            active_fingerprint,
        ):
            if text != current.get(fid, {}).get("reason", ""):
                return True
    return False
