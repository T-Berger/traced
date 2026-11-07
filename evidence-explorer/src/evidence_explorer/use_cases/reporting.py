"""Readiness for an evidence dossier, using the existing pinned review workflow.

This module performs no network request, filesystem write, platform submission,
or legal/policy determination. Artifact availability must be supplied by the
existing verified artifact service; absent availability stays unknown.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from datetime import date
from typing import Any

from ..review.contracts import Fragment, Record
from ..review.workflow import assessment_context, assessment_decisions, brief_projection
from .contracts import DossierReadiness, PolicyReference


def _projection(view: Mapping[str, Any]) -> dict[str, Any]:
    """Use the shared UI/brief rules; never promote historical decisions ourselves."""
    return brief_projection(view)


def _audit_evidence(view: Mapping[str, Any], projection: Mapping[str, Any]) -> dict:
    bundle = view.get("bundle", {})
    records = {row["id"]: row for row in bundle.get("records", [])}
    fragments = {row["id"]: row for row in bundle.get("fragments", [])}
    invalid = set()
    for record_id, raw in records.items():
        try:
            Record.model_validate(raw)
        except ValueError:
            invalid.add(record_id)
    if len(records) != len(bundle.get("records", [])):
        invalid.add("duplicate_record_ids")
    missing = set()
    all_missing = set()
    for item in (*projection["included"], *projection["omitted"]):
        citations = item["evidence"]
        valid_count = 0
        for citation in citations:
            rid, fid = citation["record_id"], citation.get("fragment_id")
            if rid not in records or rid in invalid:
                invalid.add(rid)
                continue
            if fid:
                raw = fragments.get(fid)
                try:
                    if raw is None:
                        raise ValueError("Missing fragment")
                    fragment = Fragment.model_validate(raw)
                    if fragment.record_id != rid:
                        raise ValueError("Citation record differs from fragment")
                    record = records[rid]
                    if fragment.selector.kind == "text":
                        start, end, text = (
                            fragment.selector.start,
                            fragment.selector.end,
                            record.get("text"),
                        )
                        if (
                            text is None
                            or start is None
                            or end is None
                            or not 0 <= start < end <= len(text)
                            or text[start:end] != fragment.quote
                        ):
                            raise ValueError("Quotation does not match saved source")
                    elif fragment.selector.kind == "image" and not record.get("artifact_ref"):
                        raise ValueError("No saved image reference")
                except ValueError:
                    invalid.add(fid)
                    continue
            valid_count += 1
        if not valid_count:
            all_missing.add(item["finding_id"])
            if item["status"] == "included":
                missing.add(item["finding_id"])
    return {
        "invalid": invalid,
        "missing": missing,
        "all_missing": all_missing,
        "records": records,
    }


def project_dossier_readiness(
    view: Mapping[str, Any],
    *,
    policy_references: Sequence[PolicyReference] = (),
    artifact_availability: Mapping[str, bool] | None = None,
) -> DossierReadiness:
    """Project current-context readiness; artifact availability is keyed by record ID."""
    references = tuple(PolicyReference.model_validate(value) for value in policy_references)
    bundle = view.get("bundle", {})
    case_id = bundle.get("case", {}).get("id")
    run_id = assessment_context(view)["base_run_id"]
    current_findings = [
        finding for finding in bundle.get("findings", []) if finding.get("run_id") == run_id
    ]
    limitations = [
        "Dossier readiness is not a platform-policy or legal verdict.",
        "Policy references do not establish applicability, eligibility, or a reporting outcome.",
        "This projection neither creates an export nor submits a report.",
    ]
    if (
        not case_id
        or not current_findings
        or not bundle.get("records")
        or not any(run.get("id") == run_id for run in bundle.get("runs", []))
    ):
        return DossierReadiness(
            status="unavailable",
            summary="Select a case with saved evidence and findings before preparing a dossier.",
            case_id=case_id,
            policy_references=references,
            limitations=tuple(limitations),
        )
    try:
        projection = _projection(view)
        decisions = assessment_decisions(view)
        audit = _audit_evidence(view, projection)
    except (KeyError, TypeError, ValueError) as exc:
        return DossierReadiness(
            status="unavailable",
            summary="Saved evidence or assessment references cannot be resolved.",
            case_id=case_id,
            invalid_reference_ids=(type(exc).__name__,),
            policy_references=references,
            limitations=tuple(limitations),
        )
    unreviewed = tuple(
        item["finding_id"]
        for item in projection["omitted"]
        if item["status"] in {"not_reviewed", "needs_review"}
    )
    withheld = tuple(
        item["finding_id"] for item in projection["omitted"] if item["status"] == "withheld"
    )
    unavailable, unknown = [], []
    for record_id, record in audit["records"].items():
        if record.get("artifact_ref"):
            available = (artifact_availability or {}).get(record_id)
            if available is False:
                unavailable.append(record_id)
            elif available is not True:
                unknown.append(record_id)
    if unknown:
        limitations.append(
            "Local artifact availability has not been supplied for every media record."
        )
    if audit["all_missing"]:
        limitations.append(
            "Some findings have no exact source reference; review them as unsupported."
        )
    encoded = json.dumps(projection, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    fingerprint = hashlib.sha256(encoded.encode("utf-8")).hexdigest()
    if audit["invalid"] or unavailable:
        status, summary = (
            "unavailable",
            "Repair invalid or unavailable evidence before exporting a dossier.",
        )
    elif unreviewed or audit["missing"] or unknown or not decisions:
        status, summary = (
            "needs_review",
            "The selected assessment still needs evidence or human review.",
        )
    else:
        status = "ready_for_export"
        summary = (
            "The reviewed assessment is ready for evidence export; policy applicability is unknown."
            if projection["included"]
            else "All current findings are withheld; the reviewed omissions can be exported."
        )
    return DossierReadiness(
        status=status,
        summary=summary,
        case_id=case_id,
        assessment_fingerprint=fingerprint,
        included_finding_ids=tuple(item["finding_id"] for item in projection["included"]),
        unreviewed_finding_ids=unreviewed,
        withheld_finding_ids=withheld,
        missing_evidence_finding_ids=tuple(sorted(audit["missing"])),
        invalid_reference_ids=tuple(sorted(audit["invalid"])),
        unavailable_record_ids=tuple(sorted(unavailable)),
        unknown_availability_record_ids=tuple(sorted(unknown)),
        current_decision_count=len(decisions),
        policy_references=references,
        limitations=tuple(limitations),
    )


def platform_reporting_references() -> tuple[PolicyReference, ...]:
    """Official references retrieved 7 November 2026; applicability remains unknown.

    Research and page-access limits are recorded in docs/PLATFORM_REPORTING_REFERENCES.md.
    No reference implies that any observed content violates a rule or qualifies for action.
    """
    return tuple(
        PolicyReference(
            platform=platform,
            title=title,
            url=url,
            retrieved_at=date(2026, 11, 7),
            version=None,
            source_status="reference_only",
        )
        for platform, title, url in (
            (
                "youtube",
                "YouTube reporting guidance",
                "https://support.google.com/youtube/answer/2802027",
            ),
            (
                "x",
                "X authenticity policy reference",
                "https://help.x.com/en/rules-and-policies/authenticity",
            ),
            (
                "reddit",
                "Reddit post and comment reporting guidance",
                "https://support.reddithelp.com/hc/en-us/articles/360058309512-How-do-I-report-a-post-or-comment",
            ),
            (
                "discord",
                "Discord reporting guidance",
                "https://discord.com/safety/360044103651-reporting-abusive-behavior-to-discord",
            ),
            (
                "telegram",
                "Telegram reporting FAQ reference",
                "https://telegram.org/faq#q-there-39s-illegal-content-on-telegram-how-do-i-take-it-down",
            ),
        )
    )
