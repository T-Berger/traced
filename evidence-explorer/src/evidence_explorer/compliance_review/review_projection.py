"""Project current evidence-backed category findings for a selected assessment.

The caller captures both arguments from one existing workspace read snapshot.
This projection makes no decisions, coverage assertions, model calls, or writes.
"""

from __future__ import annotations

from collections import defaultdict
from typing import Any

from ..review.contracts import EvidenceBundle
from ..review.workflow import assessment_context, assessment_decisions, evidence_fingerprint
from .matrix import CATEGORIES, _month


def project_category_review(
    view: dict[str, Any], bundle: EvidenceBundle | dict, *, record_kinds: list[str], target: str
) -> dict[str, Any]:
    """Describe current category proposals and decisions, pinned to a saved assessment.

    The selected record kinds are explicit and appear in the output. Counts are
    observed unique saved record IDs, never estimated platform coverage. Distinct
    category findings may be reviewed differently for the same record, so a
    record can appear in multiple decision counts. Incomplete coverage always
    leaves the complete/comparable counts unavailable.
    """
    if (
        not isinstance(record_kinds, list)
        or not record_kinds
        or any(not isinstance(kind, str) or not kind.strip() for kind in record_kinds)
    ):
        raise ValueError("Explicitly select at least one saved record kind.")
    if len(set(record_kinds)) != len(record_kinds):
        raise ValueError("Record kinds must be distinct.")
    if not isinstance(target, str) or not target.strip():
        raise ValueError("Explicitly select the exact analysis target.")
    complete = EvidenceBundle.model_validate(
        bundle.model_dump(mode="json") if isinstance(bundle, EvidenceBundle) else bundle
    )
    data = complete.model_dump(mode="json")
    visible = view.get("bundle") or {}
    if (visible.get("case") or {}).get("id") != complete.case.id:
        raise ValueError("The selected assessment and saved case do not match.")
    if view.get("evidence_fingerprint") != evidence_fingerprint(data):
        raise ValueError("The assessment and evidence snapshot differ. Reload before reviewing.")

    context = assessment_context(view)
    current_run = context.get("base_run_id")
    decisions = assessment_decisions(view)
    impact = view.get("impact")
    if not isinstance(impact, dict) or not isinstance(impact.get("excluded_record_ids"), list):
        raise ValueError("The assessment's excluded-record scope is unavailable.")
    excluded = set(impact["excluded_record_ids"])
    runs = {run.id: run for run in complete.runs}
    records = {record.id: record for record in complete.records}
    fragments = {fragment.id: fragment for fragment in complete.fragments}
    links = defaultdict(list)
    for link in complete.links:
        if link.target.type == "finding" and link.relation == "supports":
            links[link.target.id].append(link)

    rows = []
    rejected = []
    for finding in complete.findings:
        details = finding.details
        if finding.run_id != current_run or details.get("domain") != "compliance_category":
            continue
        if details.get("target") != target:
            continue
        run = runs[finding.run_id]
        record = records.get(details.get("record_id"))
        reason = None
        if run.status != "completed":
            reason = "Analysis run is not complete"
        elif finding.kind != "inference" or details.get("category") not in CATEGORIES:
            reason = "Unrecognized category finding"
        elif not record or record.kind not in record_kinds or record.text is None:
            reason = "Saved text record is unavailable or outside the selected record kinds"
        elif details.get("record_content_hash") != record.content_hash:
            reason = "Finding is not bound to this saved record version"
        elif not isinstance(details.get("target"), str) or not details["target"].strip():
            reason = "Analysis target is unavailable"
        elif details.get("origin") != "model" or not details.get("model"):
            reason = "Model provenance is unavailable"
        if reason:
            rejected.append({"finding_id": finding.snapshot_id, "reason": reason})
            continue

        support = []
        for link in links[finding.snapshot_id]:
            if link.run_id is not None and link.run_id != finding.run_id:
                continue
            if link.source.type == "record":
                if link.source.id != record.id:
                    continue
            elif link.source.type == "fragment":
                if fragments[link.source.id].record_id != record.id:
                    continue
            else:
                continue
            fragment_ids = set(link.fragment_ids)
            if link.source.type == "fragment":
                fragment_ids.add(link.source.id)
            support.extend(
                fragments[fid].model_dump(mode="json")
                for fid in sorted(fragment_ids)
                if fragments[fid].record_id == record.id and fragments[fid].selector.kind == "text"
            )
        if not support:
            rejected.append(
                {"finding_id": finding.snapshot_id, "reason": "No exact supporting text fragment"}
            )
            continue
        decision = decisions.get(finding.snapshot_id)
        disposition = decision.get("disposition") if decision else None
        if disposition not in {None, "use", "qualify", "withhold"}:
            disposition = None
        rows.append(
            {
                "record_id": record.id,
                "record_kind": record.kind,
                "record_content_hash": record.content_hash,
                "published_at": record.published_at,
                "text": record.text,
                "source_label": record.source_label,
                "record_synthetic": record.metadata.get("synthetic"),
                "finding_id": finding.snapshot_id,
                "run_id": finding.run_id,
                "method": run.method,
                "method_version": run.method_version,
                "category": details["category"],
                "target": details["target"],
                "model": details["model"],
                "fragments": support,
                "disposition": disposition,
                "decision": decision,
                "excluded": record.id in excluded,
            }
        )

    groups: dict[str, dict[str, dict[str, set[str]]]] = defaultdict(
        lambda: defaultdict(lambda: defaultdict(set))
    )
    undated = set()
    for row in rows:
        if row["excluded"]:
            continue
        month = _month(row["published_at"])
        if not month:
            undated.add(row["record_id"])
            continue
        label = row["disposition"] or "unreviewed"
        groups[month][row["category"]][label].add(row["record_id"])
    months = []
    for month in sorted(groups):
        categories = {}
        for category in CATEGORIES:
            categories[category] = {
                state: {
                    "observed_count": len(groups[month][category][state]),
                    "record_ids": sorted(groups[month][category][state]),
                }
                for state in ("use", "qualify", "withhold", "unreviewed")
            }
        months.append({"month": month, "coverage": "unknown", "categories": categories})
    return {
        "case_id": complete.case.id,
        "assessment": context,
        # The dossier must also suppress off-scope category statements from the
        # general brief; those statements are not part of the selected target.
        "all_category_finding_ids": sorted(
            finding.snapshot_id
            for finding in complete.findings
            if finding.run_id == current_run
            and finding.details.get("domain") == "compliance_category"
        ),
        "record_kinds": list(record_kinds),
        "target": target,
        "count_unit": "distinct saved record IDs per category and decision",
        "time_basis": "published_at in UTC",
        "months": months,
        "rows": rows,
        "undated_record_ids": sorted(undated),
        "invalid_findings": rejected,
        "limitations": [
            "Observed saved records only; monthly sample completeness is not established.",
            "Use, Qualify and Withhold are review decisions on exact findings, not legal verdicts.",
            "The same record may appear in multiple categories or decision groups.",
        ],
    }
