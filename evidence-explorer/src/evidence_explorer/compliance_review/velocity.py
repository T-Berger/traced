"""Manual, coverage-gated comparison of reviewed category findings.

The supplied view, complete bundle and planning state must be captured by the
caller from one trusted workspace read snapshot. No state or jobs are created.
"""

from __future__ import annotations

from typing import Any

from ..planning_briefs.models import BriefState
from ..review.contracts import EvidenceBundle
from ..use_cases.alerts import evaluate_activity_alert
from ..use_cases.contracts import ActivityWindow
from ..use_cases.saved_activity import SavedActivityRequest, evaluate_saved_activity
from .matrix import CATEGORIES
from .review_projection import project_category_review


def category_metric(category: str, target: str) -> str:
    """Return the explicit metric label a saved plan must use for this comparison."""
    if category not in CATEGORIES or not isinstance(target, str) or not target.strip():
        raise ValueError("Choose one category and exact target.")
    return f"Reviewed category findings selected for use: {category}; target: {target}"


def evaluate_category_velocity(
    bundle: EvidenceBundle,
    state: BriefState | None,
    view: dict[str, Any],
    request: SavedActivityRequest,
    *,
    category: str,
    target: str,
) -> dict[str, Any]:
    """Compare current Use decisions for one category, target and assessment.

    Saved activity supplies the confirmed windows, rule and immutable coverage;
    every saved record in both windows also needs at least one exact, supported,
    current Use or Withhold decision for this category. Qualify, missing and
    contradictory decisions are unavailable rather than negative labels.
    This conservative gate does not assert completeness of real-world data.
    """
    if category not in CATEGORIES:
        raise ValueError("Choose one explicit category.")
    if not isinstance(target, str) or not target.strip():
        raise ValueError("Choose the exact analysis target.")
    request = SavedActivityRequest.model_validate(request)
    limits = [
        "Counts measure saved records with Use decisions on exact category findings, "
        "not verified violations, prevalence, or platform compliance.",
        "Withhold means a finding was not selected for use; it is not a negative category label.",
        "Declared coverage describes only the bounded saved sample, not the whole platform.",
        "A comparison requests human review and creates no alert, job or decision.",
    ]
    result: dict[str, Any] = {
        "status": "unavailable",
        "category": category,
        "target": target,
        "record_kind": request.record_kind,
        "timestamp_basis": request.timestamp_basis,
        "rule": request.rule.model_dump(mode="json"),
        "baseline_record_ids": [],
        "observation_record_ids": [],
        "reviewed_findings": [],
        "evaluation": None,
        "limitations": limits,
    }

    def unavailable(reason: str) -> dict[str, Any]:
        return {**result, "summary": reason}

    if request.rule.metric != category_metric(category, target):
        return unavailable(
            "The confirmed rule metric does not explicitly match this category, target and "
            "reviewed-Use measure. Save and confirm a matching rule before evaluating."
        )
    source = evaluate_saved_activity(bundle, state, request)
    result.update(
        case_id=source.case_id,
        evidence_fingerprint=source.evidence_fingerprint,
        scope_fingerprint=source.scope_fingerprint,
        brief_id=source.brief_id,
        revision_id=source.revision_id,
        source_context_fingerprint=source.context_fingerprint,
    )
    if source.baseline is None or source.observation is None:
        return unavailable(source.summary)
    try:
        projected = project_category_review(
            view, bundle, record_kinds=[request.record_kind], target=target
        )
    except (TypeError, ValueError, KeyError):
        return unavailable(
            "The selected assessment and saved evidence do not form a current snapshot."
        )
    result["assessment"] = projected["assessment"]
    if projected["invalid_findings"]:
        return unavailable(
            "Some category findings have unavailable or incompatible exact evidence."
        )
    raw_windows = (source.baseline, source.observation)
    required = set(source.baseline_record_ids) | set(source.observation_record_ids)
    grouped: dict[str, list[dict[str, Any]]] = {record_id: [] for record_id in required}
    for row in projected["rows"]:
        if row["category"] == category and row["record_id"] in grouped:
            grouped[row["record_id"]].append(row)
    for rows in grouped.values():
        if not rows or any(row["excluded"] for row in rows):
            return unavailable(
                "Every in-window saved record needs a current included category review."
            )
        decisions = {row["disposition"] for row in rows}
        if len(decisions) != 1 or not decisions <= {"use", "withhold"}:
            return unavailable(
                "Missing, qualified, or conflicting category reviews are unavailable."
            )
        if any(
            not isinstance((row.get("decision") or {}).get(field), str)
            or not row["decision"][field].strip()
            for row in rows
            for field in ("id", "reviewer", "reason")
        ):
            return unavailable("An attributed, exact saved reviewer decision is unavailable.")
    selected = {record_id for record_id, rows in grouped.items() if rows[0]["disposition"] == "use"}
    result["reviewed_findings"] = [
        {
            "record_id": record_id,
            "record_content_hash": row["record_content_hash"],
            "finding_id": row["finding_id"],
            "run_id": row["run_id"],
            "decision_id": row["decision"]["id"],
            "disposition": row["disposition"],
            "fragment_ids": [fragment["id"] for fragment in row["fragments"]],
        }
        for record_id in sorted(grouped)
        for row in grouped[record_id]
    ]
    windows = []
    for raw in raw_windows:
        ids = tuple(sorted(selected.intersection(raw.record_ids)))
        windows.append(
            ActivityWindow.model_validate(
                {
                    **raw.model_dump(mode="json"),
                    "record_ids": ids,
                    "observed_count": len(ids),
                }
            )
        )
    result["baseline_record_ids"] = list(windows[0].record_ids)
    result["observation_record_ids"] = list(windows[1].record_ids)
    evaluation = evaluate_activity_alert(request.rule, *windows)
    result.update(
        status=evaluation.status,
        summary=evaluation.summary,
        evaluation=evaluation.model_dump(mode="json"),
    )
    return result
