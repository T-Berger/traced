"""Pure descriptions of captured planning history, without granting local authority.

The transport history contains revisions and attributed confirmations. It does
not identify the current local head, active plan, or locally applicable approval.
Unbound ancestor revisions are retained only to explain a bound revision's chain.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from ...planning_briefs.models import PlanningHistory, canonical_json
from ...planning_briefs.scope import validate_history_for_bundle
from ..contracts import EvidenceBundle
from .packages import CapturedCasePackage


def planning_summary(captured: CapturedCasePackage) -> dict[str, Any]:
    """Describe only the evidence/planning values already captured by the caller."""
    bundle = EvidenceBundle.model_validate(captured.bundle)
    history = (
        PlanningHistory.model_validate(captured.planning_history)
        if captured.planning_history is not None
        else PlanningHistory()
    )
    if history.revisions or history.confirmations:
        validate_history_for_bundle(history, bundle)
    confirmations = [
        {
            **confirmation.model_dump(mode="json"),
            "attribution": "recorded_confirmation",
            "grants_local_approval": False,
        }
        for confirmation in sorted(
            history.confirmations,
            key=lambda row: (row.brief_id, row.revision_id, row.confirmed_at, row.confirmation_id),
        )
    ]
    revisions = []
    for revision in sorted(
        history.revisions, key=lambda row: (row.brief_id, row.number, row.revision_id)
    ):
        binding = revision.binding
        revisions.append(
            {
                "brief_id": revision.brief_id,
                "revision_id": revision.revision_id,
                "number": revision.number,
                "parent_revision_id": revision.parent_revision_id,
                "author": revision.author,
                "created_at": revision.model_dump(mode="json")["created_at"],
                "content_hash": revision.content_hash,
                "fields": revision.fields.model_dump(mode="json"),
                "binding": {
                    "case_id": binding.case_id,
                    "source_case_fingerprint": binding.source_case_fingerprint,
                    "evidence_fingerprint": binding.scope.evidence_fingerprint,
                    "scope_fingerprint": binding.scope.scope_fingerprint,
                    "basis": binding.scope.basis,
                    "source_refs": list(binding.scope.source_refs),
                }
                if binding
                else None,
                "binding_status": "bound" if binding else "unbound_ancestor",
                "confirmation_ids": [
                    item["confirmation_id"]
                    for item in confirmations
                    if item["revision_id"] == revision.revision_id
                ],
            }
        )
    brief_ids = sorted({revision["brief_id"] for revision in revisions})
    counts = {
        "briefs": len(brief_ids),
        "revisions": len(revisions),
        "bound_revisions": sum(revision["binding"] is not None for revision in revisions),
        "unbound_ancestors": sum(revision["binding"] is None for revision in revisions),
        "confirmations": len(confirmations),
    }
    return {
        "status": "history_available" if revisions else "not_recorded",
        "case_id": bundle.case.id,
        "context_token": captured.planning_context_token,
        "counts": counts,
        "brief_ids": brief_ids,
        "revisions": revisions,
        "confirmations": confirmations,
        "current_local_state": "not_projected",
        "summary": (
            f"Captured planning history contains {counts['briefs']} brief lineage(s), "
            f"{counts['revisions']} revision(s), and {counts['confirmations']} attributed "
            "confirmation record(s)."
            if revisions
            else "No planning revision history was captured for this case."
        ),
        "limitations": [
            "This history does not identify the current local head or active plan. It does "
            "not establish current local confirmation, scope applicability, or approval.",
            "Confirmation authors, dates, and recorded provenance are historical attribution. "
            "Even recorded local provenance does not grant approval in the receiving workspace.",
            "Unbound ancestor revisions preserve the lineage of a bound revision; they are "
            "not independently bound plans for this case.",
            "The complete captured planning history travels in the case package. Evidence-only "
            "JSON does not contain the planning sidecar.",
        ],
    }


def _recorded_values(value: Any) -> str:
    return "Not recorded" if value is None or value == [] else f"`{canonical_json(value)}`"


def _recorded_window(window: Mapping[str, Any] | None) -> str:
    if window is None:
        return "Not recorded"
    return (
        f"`{window['id']}` · start: {window.get('start') or 'Not recorded'} · "
        f"end: {window.get('end') or 'Not recorded'}"
    )


def planning_dossier_section(summary: Mapping[str, Any]) -> str:
    """Render captured attribution without inferring a latest or approved revision."""
    lines = [
        "## Saved investigation planning history",
        "",
        summary["summary"],
        "",
        "**Historical attribution only. Current local head, active-plan status, and local "
        "approval are not established by this history.**",
        "",
    ]
    if not summary["revisions"]:
        lines += ["Planning history: Not recorded for the selected case.", ""]
    for revision in summary["revisions"]:
        binding, fields = revision["binding"], revision["fields"]
        lines += [
            f"### Brief `{revision['brief_id']}` · revision {revision['number']}",
            "",
            f"Revision: `{revision['revision_id']}`",
            f"Parent revision: `{revision['parent_revision_id']}`"
            if revision["parent_revision_id"]
            else "Parent revision: none (first revision).",
            f"Authored by: {revision['author']} · {revision['created_at']}",
            f"Revision content hash: `{revision['content_hash']}`",
            f"Recorded purpose: {fields.get('purpose') or 'Not supplied'}",
            f"Recorded use case: {fields.get('use_case_id') or 'Not supplied'}",
            f"Recorded reviewer role: {fields.get('reviewer_role') or 'Not supplied'}",
            f"Intended output: {fields.get('expected_output') or 'Not supplied'}",
            "Recorded public subjects/topics: "
            + _recorded_values(fields.get("public_subjects_or_topics")),
            "Recorded source selection: " + _recorded_values(fields.get("source_selection")),
            "Recorded languages: " + _recorded_values(fields.get("languages")),
            f"Recorded information scope: {fields.get('information_scope') or 'Not recorded'}",
            "Recorded baseline window: " + _recorded_window(fields.get("baseline_window")),
            "Recorded observation window: " + _recorded_window(fields.get("observation_window")),
            "Recorded alert-rule configuration (not evaluated): "
            + _recorded_values(fields.get("alert_rule")),
        ]
        if binding:
            lines += [
                f"Saved binding: case `{binding['case_id']}` · {binding['basis']}",
                f"Source case fingerprint: `{binding['source_case_fingerprint']}`",
                f"Evidence fingerprint: `{binding['evidence_fingerprint']}`",
                f"Source scope fingerprint: `{binding['scope_fingerprint']}`",
                f"Saved source references: {len(binding['source_refs'])}; exact identifiers "
                "and scope metadata are preserved in the case package.",
            ]
        else:
            lines.append("Unbound ancestor: retained to explain a later bound revision.")
        if revision["confirmation_ids"]:
            lines.append(
                "Attributed confirmation records: "
                + ", ".join(f"`{value}`" for value in revision["confirmation_ids"])
                + "."
            )
        else:
            lines.append("No confirmation record is attached to this captured revision.")
        lines.append("")
    if summary["confirmations"]:
        lines += ["### Attributed confirmation records", ""]
        for confirmation in summary["confirmations"]:
            lines += [
                f"- `{confirmation['confirmation_id']}` · brief `{confirmation['brief_id']}` · "
                f"revision `{confirmation['revision_id']}`",
                f"  Recorded reviewer: {confirmation['reviewer']} · {confirmation['confirmed_at']}",
                f"  Recorded provenance: {confirmation['provenance']}; this does not grant "
                "local approval or select the current plan.",
                f"  Confirmed binding digest: `{confirmation['binding_digest']}`",
            ]
        lines.append("")
    lines.extend(f"- {limitation}" for limitation in summary["limitations"])
    lines.append("")
    return "\n".join(lines)
