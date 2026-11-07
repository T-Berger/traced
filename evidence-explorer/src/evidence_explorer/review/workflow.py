"""Pure shared assessment queries for the screen and the exported briefing.

Only a decision pinned to the selected evidence and assessment version is current.
Historical reviews remain available in the case package; they never silently
become approvals for another scenario, restored source set, or changed case.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from typing import Any

from .origins import claim_origin_label

Data = Mapping[str, Any]


def case_type(bundle: Data) -> str:
    """Select specialist presentation without changing common review semantics."""
    if bundle.get("swarm_context") or any(
        finding.get("details", {}).get("domain") == "swarm"
        for finding in bundle.get("findings", [])
    ):
        return "swarm"
    if any(record.get("artifact_ref") for record in bundle.get("records", [])):
        return "media"
    if bundle.get("case", {}).get("id", "").startswith("sources:"):
        return "sources"
    return "investigation"


def evidence_fingerprint(bundle: Data) -> str:
    """Pin reopened decisions to evidence while allowing history to be packaged."""
    evidence = {key: value for key, value in bundle.items() if key not in {"reviews", "scenarios"}}
    encoded = json.dumps(evidence, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def assessment_context(view: Data) -> dict[str, Any]:
    scenario = view.get("scenario") or view.get("baseline_scenario") or {}
    baseline = view.get("is_baseline", not bool(view.get("scenario")))
    run_id = scenario.get("base_run_id") or next(
        (run["id"] for run in view.get("bundle", {}).get("runs", [])), None
    )
    return {
        "id": scenario.get("id"),
        "label": "Baseline" if baseline else scenario.get("label", "Scenario"),
        "version": scenario.get("version", 1),
        "base_run_id": run_id,
        "is_baseline": bool(baseline),
        "imported": bool(scenario.get("imported")),
    }


def assessment_decisions(view: Data) -> dict[str, dict[str, Any]]:
    """Return the latest applicable decision for each visible finding snapshot."""
    bundle = view.get("bundle", {})
    context = assessment_context(view)
    findings = {
        finding["snapshot_id"]: finding
        for finding in bundle.get("findings", [])
        if finding["run_id"] == context["base_run_id"]
    }
    current: dict[str, dict[str, Any]] = {}
    scenarios = {scenario["id"]: scenario for scenario in bundle.get("scenarios", [])}
    scope_fingerprint = view.get("evidence_fingerprint") or evidence_fingerprint(bundle)
    for review in sorted(bundle.get("reviews", []), key=lambda row: row.get("timestamp", "")):
        finding_id = review["finding_snapshot_id"]
        if finding_id not in findings or review["run_id"] != context["base_run_id"]:
            continue
        imported = scenarios.get(review.get("scenario_id"), {})
        selected = review.get("scenario_id") == context["id"]
        baseline = (
            context["is_baseline"]
            and imported.get("assessment_kind") == "baseline"
            and imported.get("base_run_id") == context["base_run_id"]
        )
        if not (selected or baseline) or review.get("evidence_fingerprint") != scope_fingerprint:
            continue
        # A saved export records whether the exact source bundle was still the
        # evidence the reviewer saw. Unknown historical provenance stays unknown.
        if not review.get("case_fingerprint") or review.get("case_fingerprint") != review.get(
            "source_case_fingerprint"
        ):
            continue
        current[finding_id] = {
            **review,
            "finding_id": finding_id,
            "created_at": review["timestamp"],
            "imported": True,
        }
    for event in view.get("events", []):
        review = event.get("payload", {})
        finding_id = review.get("finding_id")
        if (
            event.get("event_type") == "finding_reviewed"
            and finding_id in findings
            and context["id"] is not None
            and review.get("scenario_id") == context["id"]
            and review.get("scenario_version") == context["version"]
            and view.get("case_fingerprint") is not None
            and review.get("case_fingerprint") == view.get("case_fingerprint")
        ):
            current[finding_id] = dict(review)
    return current


def _citations(bundle: Data, finding_id: str, excluded: set[str]) -> list[dict[str, Any]]:
    """Collect exact fragments through declared premise links, without truncation."""
    records = {record["id"]: record for record in bundle.get("records", [])}
    fragments = {fragment["id"]: fragment for fragment in bundle.get("fragments", [])}
    finding = next(
        (item for item in bundle.get("findings", []) if item["snapshot_id"] == finding_id), {}
    )
    details = finding.get("details", {})
    claim_check = details.get("domain") == "claim_check"
    assessment = details.get("assessment", {}) if claim_check else {}
    roles = {
        citation["fragment_id"]: citation["stance"] for citation in assessment.get("citations", [])
    }
    all_roles: dict[str, set[str]] = {}
    for citation in assessment.get("citations", []):
        all_roles.setdefault(citation["fragment_id"], set()).add(citation["stance"])
    for objection in assessment.get("metadata", {}).get("challenge", {}).get("findings", []):
        for fragment_id in objection.get("fragment_ids", []):
            roles.setdefault(fragment_id, "candidate_objection")
            all_roles.setdefault(fragment_id, set()).add("candidate_objection")
    links: dict[str, list[Data]] = {}
    for link in bundle.get("links", []):
        if link["target"]["type"] == "finding" and link["relation"] in {"supports", "depends_on"}:
            links.setdefault(link["target"]["id"], []).append(link)
    selected_records: set[str] = set()
    selected_fragments: set[str] = set(roles) if claim_check else set()
    pending, visited = [finding_id], set()
    while pending:
        current = pending.pop()
        if current in visited:
            continue
        visited.add(current)
        for link in links.get(current, []):
            source = link["source"]
            if source["type"] == "finding":
                pending.append(source["id"])
            elif source["type"] == "record":
                selected_records.add(source["id"])
            elif source["type"] == "fragment":
                selected_fragments.add(source["id"])
            selected_fragments.update(link.get("fragment_ids", []))
    result, grouped_spans = [], {}
    ordered_fragments = (
        sorted(
            selected_fragments,
            key=lambda fid: (
                0
                if roles.get(fid) in {"supports", "contradicts", "context"}
                else 1
                if roles.get(fid) == "candidate_objection"
                else 2,
                fid,
            ),
        )
        if claim_check
        else sorted(selected_fragments)
    )
    for fragment_id in ordered_fragments:
        fragment = fragments[fragment_id]
        record_id = fragment["record_id"]
        selected_records.add(record_id)
        if record_id in excluded:
            continue
        span = (record_id, json.dumps(fragment["selector"], sort_keys=True))
        evidence_role = roles.get(fragment_id, "assessment_input" if claim_check else None)
        evidence_roles = sorted(
            all_roles.get(fragment_id, {evidence_role} if evidence_role else set())
        )
        if claim_check and span in grouped_spans:
            # A repeated quotation may carry opposing stances or a second-pass
            # objection. Combine presentation only; retain every role and ID.
            existing = grouped_spans[span]
            existing["fragment_ids"].append(fragment_id)
            existing["evidence_roles"] = sorted(
                set(existing["evidence_roles"]) | set(evidence_roles)
            )
            continue
        record = records[record_id]
        citation = {
            "record_id": record_id,
            "source_label": record["source_label"],
            "fragment_id": fragment_id,
            "fragment_ids": [fragment_id],
            "quote": fragment.get("quote"),
            "selector": fragment["selector"],
            "source_url": record.get("source_url"),
            "artifact_ref": record.get("artifact_ref"),
            "record_synthetic": record.get("metadata", {}).get("synthetic"),
            "evidence_role": evidence_role,
            "evidence_roles": evidence_roles,
        }
        result.append(citation)
        grouped_spans[span] = citation
    quoted_records = {citation["record_id"] for citation in result}
    for record_id in sorted(selected_records - quoted_records - excluded):
        record = records[record_id]
        result.append(
            {
                "record_id": record_id,
                "source_label": record["source_label"],
                "fragment_id": None,
                "fragment_ids": [],
                "quote": None,
                "selector": {"kind": "whole_record"},
                "source_url": record.get("source_url"),
                "artifact_ref": record.get("artifact_ref"),
                "record_synthetic": record.get("metadata", {}).get("synthetic"),
                "evidence_role": "assessment_input" if claim_check else None,
                "evidence_roles": ["assessment_input"] if claim_check else [],
            }
        )
    return result


def brief_projection(view: Data) -> dict[str, Any]:
    """Project one assessment into included statements and explicit omissions."""
    bundle = view.get("bundle", {})
    context = assessment_context(view)
    decisions = assessment_decisions(view)
    affected = set(view.get("impact", {}).get("affected_finding_ids", []))
    excluded = set(view.get("impact", {}).get("excluded_record_ids", []))
    imported_scenarios = {scenario["id"]: scenario for scenario in bundle.get("scenarios", [])}
    included, omitted = [], []
    for finding in bundle.get("findings", []):
        if finding["run_id"] != context["base_run_id"]:
            continue
        finding_id = finding["snapshot_id"]
        review = decisions.get(finding_id)
        prior = any(
            event.get("event_type") == "finding_reviewed"
            and event.get("payload", {}).get("finding_id") == finding_id
            and event.get("payload", {}).get("scenario_id") == context["id"]
            for event in view.get("events", [])
        )
        prior = prior or any(
            review["finding_snapshot_id"] == finding_id
            and (
                review.get("scenario_id") == context["id"]
                or (
                    context["is_baseline"]
                    and imported_scenarios.get(review.get("scenario_id"), {}).get("assessment_kind")
                    == "baseline"
                )
            )
            for review in bundle.get("reviews", [])
        )
        if review:
            status = "withheld" if review["disposition"] == "withhold" else "included"
            reason = review["reason"]
        elif prior:
            status, reason = (
                "needs_review",
                "Earlier decision requires review for this assessment version.",
            )
        elif finding_id in affected:
            status, reason = (
                "needs_review",
                "Evidence changed; no decision saved for this assessment.",
            )
        else:
            status, reason = "not_reviewed", "No decision saved for this assessment."
        item = {
            "finding_id": finding_id,
            "statement": finding["statement"],
            "kind": finding["kind"],
            "run_id": finding["run_id"],
            "review": review,
            "disposition": review["disposition"] if review else None,
            "reason": reason,
            "status": status,
            "evidence": _citations(bundle, finding_id, excluded),
        }
        item["record_ids"] = sorted({citation["record_id"] for citation in item["evidence"]})
        if (origin := claim_origin_label(finding)) is not None:
            item["claim_origin"] = origin
        (included if status == "included" else omitted).append(item)
    case = bundle.get("case", {})
    return {
        "title": case.get("title", "Evidence brief"),
        "assessment_label": context["label"],
        "synthetic": bool(case.get("synthetic")),
        "claim_check": any(
            finding.get("details", {}).get("domain") == "claim_check"
            for finding in bundle.get("findings", [])
        ),
        "case": case,
        "assessment": context,
        "included": included,
        "omitted": omitted,
        "excluded_record_ids": sorted(excluded),
        "limitations": list(case.get("limitations", [])),
        "coverage": case.get("coverage", {}),
        "methods": [run for run in bundle.get("runs", []) if run["id"] == context["base_run_id"]],
    }
