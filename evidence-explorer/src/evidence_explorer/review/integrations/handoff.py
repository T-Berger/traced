"""Read-only selected-assessment handoffs and draft evidence dossiers.

Purpose selection is a session guide, never a saved or confirmed investigation
brief. Media availability is supplied by the verified artifact service. This
module neither accesses files nor contacts a model, source, or reporting platform.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from copy import deepcopy
from typing import Any

from ...use_cases.catalog import get_use_case
from ...use_cases.reporting import platform_reporting_references, project_dossier_readiness
from ..contracts import EvidenceBundle
from ..workflow import assessment_decisions, brief_projection
from .briefs import render_brief


def _media(bundle: Mapping[str, Any], availability: Mapping[str, bool] | None) -> dict:
    supplied = availability or {}
    return {
        "media_records": [
            {
                "record_id": record["id"],
                "artifact_ref": record["artifact_ref"],
                "availability": "available"
                if supplied.get(record["id"]) is True
                else "unavailable"
                if supplied.get(record["id"]) is False
                else "unknown",
            }
            for record in bundle.get("records", [])
            if record.get("artifact_ref")
        ]
    }


def _counts(bundle: Mapping[str, Any], evidence: dict) -> dict:
    media = evidence["media_records"]
    return {
        "records": len(bundle.get("records", [])),
        "fragments": len(bundle.get("fragments", [])),
        "runs": len(bundle.get("runs", [])),
        "media": len(media),
        **{
            f"{status}_media": sum(item["availability"] == status for item in media)
            for status in ("available", "unavailable", "unknown")
        },
    }


def _purpose(purpose_id: str | None) -> dict:
    purpose = get_use_case(purpose_id) if purpose_id else None
    return {
        "id": purpose.id if purpose else None,
        "title": purpose.title if purpose else "No purpose selected",
        "status": "session_only_unconfirmed" if purpose else "not_selected",
        "confirmed": False,
        "persisted": False,
        "scope_notes": list(purpose.scope_notes) if purpose else [],
        "note": (
            "This purpose is a session-only guide. It is not a saved, confirmed investigation "
            "brief and does not establish scope, policy eligibility, or approval."
        ),
    }


def _record_findings(bundle: Mapping[str, Any], projection: dict) -> dict[str, str]:
    items = [*projection["included"], *projection["omitted"]]
    current = {item["finding_id"] for item in items}
    result = {}
    for item in items:
        for record_id in item["record_ids"]:
            result.setdefault(record_id, item["finding_id"])
    fragments = {fragment["id"]: fragment["record_id"] for fragment in bundle.get("fragments", [])}
    for link in bundle.get("links", []):
        target = link.get("target", {})
        if target.get("type") != "finding" or target.get("id") not in current:
            continue
        source = link.get("source", {})
        record_ids = []
        if source.get("type") == "record":
            record_ids.append(source["id"])
        elif source.get("type") == "fragment" and source.get("id") in fragments:
            record_ids.append(fragments[source["id"]])
        record_ids.extend(
            fragments[fid] for fid in link.get("fragment_ids", []) if fid in fragments
        )
        for record_id in record_ids:
            result.setdefault(record_id, target["id"])
    return result


def project_handoff(
    view: Mapping[str, Any],
    *,
    purpose_id: str | None = None,
    dirty: bool = False,
    artifact_availability: Mapping[str, bool] | None = None,
) -> dict:
    """Explain existing saved decisions and gaps without changing their applicability."""
    projection = brief_projection(view)
    bundle = view.get("bundle", {})
    purpose = _purpose(purpose_id)
    references = (
        platform_reporting_references() if purpose_id == "platform_evidence_dossier" else ()
    )
    readiness = project_dossier_readiness(
        view, policy_references=references, artifact_availability=artifact_availability
    ).model_dump(mode="json")
    evidence = _media(bundle, artifact_availability)
    counts = {
        **_counts(bundle, evidence),
        "findings": len(projection["included"]) + len(projection["omitted"]),
        "current_decisions": len(assessment_decisions(view)),
        "included": len(projection["included"]),
        **{
            key: sum(item["status"] == status for item in projection["omitted"])
            for key, status in (
                ("withheld", "withheld"),
                ("unreviewed", "not_reviewed"),
                ("stale", "needs_review"),
            )
        },
    }
    gaps = []
    record_findings = _record_findings(bundle, projection)

    def gap(kind, label, detail, action, *, finding_id=None, record_id=None):
        gaps.append(
            {
                "id": f"{kind}:{finding_id or record_id or 'assessment'}",
                "kind": kind,
                "label": label,
                "detail": detail,
                "action": action,
                "finding_id": finding_id,
                "record_id": record_id,
            }
        )

    if dirty:
        gap(
            "unsaved_draft",
            "Save or discard unsaved notes",
            "The handoff below reflects saved decisions only. Draft wording is not an approval "
            "and cannot be exported through this draft-dossier action.",
            "save_draft",
        )
    if purpose_id:
        gap(
            "unconfirmed_purpose",
            "Confirm the investigation purpose and scope",
            purpose["note"],
            "check_scope",
        )
    if not counts["records"]:
        gap(
            "missing_records",
            "Add saved evidence to this investigation",
            "No saved source records are available for a checkable handoff.",
            "check_scope",
        )
    if not counts["findings"]:
        gap(
            "missing_assessment",
            "Add source-linked findings for review",
            "The selected assessment has no findings to include or explain as omissions.",
            "check_scope",
        )
    for item in projection["omitted"]:
        if item["status"] != "withheld":
            gap(
                item["status"],
                "Review this finding for the selected assessment",
                item["reason"],
                "review_finding",
                finding_id=item["finding_id"],
            )
    for finding_id in readiness["missing_evidence_finding_ids"]:
        gap(
            "missing_evidence",
            "Inspect a statement without exact source support",
            "A saved briefing decision exists, but no valid remaining evidence reference "
            "supports including it. Revisit the wording, evidence, or disposition.",
            "review_finding",
            finding_id=finding_id,
        )
    fragment_records = {item["id"]: item["record_id"] for item in bundle.get("fragments", [])}
    record_ids = {item["id"] for item in bundle.get("records", [])}
    for reference_id in readiness["invalid_reference_ids"]:
        record_id = (
            reference_id if reference_id in record_ids else fragment_records.get(reference_id)
        )
        gap(
            "invalid_reference",
            "Repair an invalid saved evidence reference",
            f"Reference {reference_id} cannot be validated against the saved evidence.",
            "inspect_record" if record_id else "check_scope",
            finding_id=record_findings.get(record_id),
            record_id=record_id,
        )
    for item in evidence["media_records"]:
        if item["availability"] == "available":
            continue
        missing = item["availability"] == "unavailable"
        gap(
            "unavailable_media" if missing else "unknown_media",
            "Restore the saved media" if missing else "Check saved media availability",
            "The saved artifact is unavailable or does not match its evidence hash."
            if missing
            else "The saved artifact has not been checked on this computer.",
            "inspect_record",
            finding_id=record_findings.get(item["record_id"]),
            record_id=item["record_id"],
        )
    if references:
        gap(
            "policy_review",
            "Review platform eligibility and reporting requirements",
            "Official references are dated reference material. Applicability and eligibility "
            "remain unknown; no report has been submitted.",
            "check_references",
        )
    saved_scope = bundle.get("collection_scope")
    if saved_scope is None:
        saved_scope = projection["coverage"].get("collection_scope")
    return deepcopy(
        {
            "case": projection["case"],
            "assessment": projection["assessment"],
            "purpose": purpose,
            "dirty": bool(dirty),
            # This concerns a saved draft document, not media-package completeness or policy.
            "export_allowed": bool(projection["case"].get("id")) and not dirty,
            "brief": projection,
            "readiness": readiness,
            "counts": counts,
            "evidence": evidence,
            "scope": {
                "basis": "saved_case_snapshot",
                "collection_scope": saved_scope,
                "coverage": projection["coverage"],
                "limitations": projection["limitations"],
            },
            "gaps": gaps,
            "references": [reference.model_dump(mode="json") for reference in references],
        }
    )


def _recomputation(bundle: dict) -> dict:
    """Inspect the exact retained-input prerequisites of the existing Swarm bridge."""
    from .swarm import _digest

    context = bundle.get("swarm_context")
    baseline_id = context.get("baseline_run_id") if isinstance(context, dict) else None
    result = {
        "available": False,
        "baseline_run_id": baseline_id,
        "runtime_status": "not_checked",
        "capability": "dependency_review_only",
        "reason": "Recomputation is unavailable: complete normalized Swarm inputs and their "
        "original baseline are not retained. Evidence inspection remains available.",
    }
    if not isinstance(context, dict) or not isinstance(context.get("dataset"), dict):
        return result
    dataset, fingerprint = context["dataset"], context.get("dataset_fingerprint")
    if _digest(dataset) != fingerprint:
        return {**result, "reason": "Swarm recomputation is unavailable: retained inputs changed."}
    baseline = next((run for run in bundle["runs"] if run["id"] == baseline_id), None)
    if (
        baseline is None
        or not baseline["full_inputs_available"]
        or baseline["input_fingerprint"] != fingerprint
        or baseline["method"] != "swarm_detector.detector.analyze"
        or baseline["recomputation_capability"] != "swarm_full_subset"
    ):
        return {
            **result,
            "reason": "Swarm recomputation is unavailable: the original baseline context "
            "does not declare complete supported Swarm inputs.",
        }
    raw = baseline.get("swarm_result", {})
    if (
        not isinstance(raw, dict)
        or raw.get("source_fingerprint") != fingerprint
        or raw.get("config") != baseline["configuration"]
        or any(not isinstance(dataset.get(key), list) for key in ("posts", "messages"))
    ):
        return {
            **result,
            "reason": "Swarm recomputation is unavailable: retained baseline provenance "
            "or normalized source context is incomplete or inconsistent.",
        }
    source_indexes = {}
    for kind, collection, key in (
        ("telegram_post", "posts", "post_key"),
        ("telegram_message", "messages", "message_key"),
    ):
        rows = dataset[collection]
        if any(not isinstance(row, dict) or not isinstance(row.get(key), str) for row in rows):
            return {
                **result,
                "reason": "Swarm recomputation is unavailable: retained source identifiers "
                "are incomplete.",
            }
        source_indexes[kind] = (key, {row[key]: row for row in rows})
        if len(source_indexes[kind][1]) != len(rows):
            return {
                **result,
                "reason": "Swarm recomputation is unavailable: retained source identifiers "
                "are not unique.",
            }
    excludable = [record for record in bundle["records"] if record["kind"] in source_indexes]
    if not excludable:
        return {
            **result,
            "reason": "Swarm recomputation is unavailable: there are no saved message "
            "or post records eligible for an exclusion.",
        }
    for record in excludable:
        key, original = source_indexes[record["kind"]]
        supplied = record["metadata"].get("swarm")
        if (
            not isinstance(supplied, dict)
            or not isinstance(supplied.get(key), str)
            or original.get(supplied[key]) != supplied
        ):
            return {
                **result,
                "reason": "Swarm recomputation is unavailable: saved exclusion records "
                "do not match retained source context.",
            }
    return {
        **result,
        "available": True,
        "capability": "swarm_full_subset",
        "reason": "The retained original Swarm baseline and complete declared input pass "
        "the saved provenance checks. Recompute only from that baseline after selecting a "
        "valid message or post exclusion and reason. The optional detector runtime is not "
        "checked by this receipt; no recomputation has run.",
    }


def project_import_receipt(
    bundle: EvidenceBundle | Mapping[str, Any],
    *,
    artifact_availability: Mapping[str, bool] | None = None,
) -> dict:
    """Describe a validated saved snapshot; never claim acceptance by a recipient."""
    saved = EvidenceBundle.model_validate(bundle).model_dump(mode="json")
    evidence = _media(saved, artifact_availability)
    return {
        "case": saved["case"],
        "validation": {
            "status": "validated_saved_snapshot",
            "recipient_acceptance": "not_recorded",
            "note": "Saved bundle structure, text hashes, and source references validate. "
            "Local media availability is reported separately. This is not recipient acceptance.",
        },
        "counts": {
            **_counts(saved, evidence),
            "assessments": len(saved["scenarios"]),
            "reviews": len(saved["reviews"]),
        },
        "assessments": saved["scenarios"],
        "runs": [
            {
                key: run[key]
                for key in (
                    "id",
                    "method",
                    "method_version",
                    "created_at",
                    "input_fingerprint",
                    "full_inputs_available",
                    "recomputation_capability",
                )
            }
            for run in saved["runs"]
        ],
        "evidence": evidence,
        "recomputation": _recomputation(saved),
        "limitations": [
            "Assessment count covers explicitly saved assessment/scenario records; analysis "
            "runs are counted separately. Imported history is not a new briefing approval.",
            "Media availability must be checked against the saved file hash on this computer.",
            "Recomputation support concerns retained Swarm provenance, not a completed rerun "
            "or a check of locally installed runtime dependencies.",
        ],
    }


def render_dossier(handoff: Mapping[str, Any]) -> str:
    """Render saved decisions plus visible outstanding requirements as a draft only."""
    if handoff.get("dirty") or not handoff.get("export_allowed"):
        raise ValueError(
            "Save or discard unsaved notes and select a case before exporting a dossier."
        )
    purpose, readiness = handoff["purpose"], handoff["readiness"]
    lines = [
        "# Draft evidence dossier",
        "",
        "**Draft for human review. Policy eligibility is unknown. Submission status: "
        "not submitted.**",
        "",
        f"Selected purpose: {purpose['title']}",
        purpose["note"],
        "",
        f"Saved-assessment readiness: {readiness['status']} — {readiness['summary']}",
        "",
        "## Draft dossier checklist and unresolved gaps",
        "",
        "- [ ] Confirm the purpose, recipient, reporting route, and investigation scope.",
        "- [ ] Review platform eligibility and requirements independently of evidence export.",
    ]
    if handoff["gaps"]:
        for gap in handoff["gaps"]:
            targets = [
                f"{kind}: `{gap[kind]}`" for kind in ("finding_id", "record_id") if gap.get(kind)
            ]
            suffix = " (" + "; ".join(targets) + ")" if targets else ""
            lines.append(f"- [ ] {gap['label']}: {gap['detail']}{suffix}")
    else:
        lines.append("No additional saved-evidence gaps were identified by this projection.")
    counts = handoff["counts"]
    saved_scope = handoff["scope"]["collection_scope"]
    lines += [
        "",
        f"Saved evidence: {counts['records']} records; {counts['fragments']} fragments. "
        f"Media: {counts['available_media']} available, {counts['unavailable_media']} unavailable, "
        f"{counts['unknown_media']} availability unknown.",
        "",
        "Saved source selection: Not recorded."
        if saved_scope is None
        else "Saved source selection: `"
        + json.dumps(saved_scope, ensure_ascii=False, sort_keys=True)
        + "`",
        "",
    ]
    if handoff["references"]:
        lines += ["## Dated official reference material", ""]
        for reference in handoff["references"]:
            lines += [
                f"- [{reference['title']}]({reference['url']}) · {reference['platform']} · "
                f"retrieved {reference.get('retrieved_at') or 'unknown'} · "
                f"version {reference.get('version') or 'not supplied'} · "
                f"{reference['source_status']} · applicability {reference['applicability']}.",
            ]
        lines += [
            "",
            "These dated references do not establish current eligibility or a reporting outcome.",
            "",
        ]
    lines += [render_brief(handoff["brief"])]
    return "\n".join(lines)
