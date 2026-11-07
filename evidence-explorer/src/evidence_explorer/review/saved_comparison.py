"""Inspect explicitly saved scenario correspondences without running analysis.

Only the exact selected native scenario supplies relationships. Review-version
aliases, matching exclusions, group names, and shared result runs imply no mapping.
"""

from __future__ import annotations

from collections.abc import Mapping
from copy import deepcopy
from typing import Any

from pydantic import ValidationError

from .contracts import EvidenceBundle, Fragment, Record
from .models import FindingCorrespondence
from .workflow import _citations

_UNCERTAINTY = (
    "A saved correspondence does not establish persistent identity or transfer reviewer decisions."
)


def _run_summary(run: dict | None) -> dict | None:
    if run is None:
        return None
    return {
        key: run.get(key)
        for key in ("id", "method", "method_version", "created_at", "status", "input_fingerprint")
    }


def _finding(bundle: dict, finding: dict) -> dict:
    issues, evidence = [], []
    records = {row["id"]: row for row in bundle.get("records", [])}
    fragments = {row["id"]: row for row in bundle.get("fragments", [])}
    try:
        supplied = _citations(bundle, finding["snapshot_id"], set())
    except (KeyError, TypeError, ValueError):
        supplied = []
        issues.append("Saved evidence references cannot be resolved for this finding.")
    for citation in supplied:
        try:
            record = Record.model_validate(records[citation["record_id"]])
            if citation["fragment_id"]:
                fragment = Fragment.model_validate(fragments[citation["fragment_id"]])
                if fragment.record_id != record.id:
                    raise ValueError("Fragment belongs to another record")
                selector = fragment.selector
                if selector.kind == "text":
                    start, end = selector.start, selector.end
                    if (
                        record.text is None
                        or start is None
                        or end is None
                        or not 0 <= start < end <= len(record.text)
                        or record.text[start:end] != fragment.quote
                    ):
                        raise ValueError("Saved quotation or offsets differ")
                elif fragment.quote is not None:
                    raise ValueError("A non-text selector does not verify a text quotation")
            evidence.append(citation)
        except (KeyError, TypeError, ValueError):
            reference_id = citation.get("fragment_id") or citation.get("record_id")
            issues.append(f"Saved evidence reference {reference_id} cannot be validated.")
    return {
        **{
            key: finding.get(key)
            for key in ("snapshot_id", "run_id", "kind", "statement", "lineage_id", "version")
        },
        "evidence": evidence,
        "record_ids": sorted({row["record_id"] for row in evidence}),
        "fragment_ids": sorted({row["fragment_id"] for row in evidence if row["fragment_id"]}),
        "issues": issues,
    }


def _correspondence(raw: Any, index: int, before: dict, after: dict) -> dict:
    raw = raw if isinstance(raw, dict) else {}
    row = {
        "index": index,
        "relation": raw.get("relation") if isinstance(raw.get("relation"), str) else "unknown",
        "basis": raw.get("basis") if isinstance(raw.get("basis"), str) else "Not supplied",
        "status": "invalid",
        "baseline_snapshot_ids": [],
        "result_snapshot_ids": [],
        "baseline_findings": [],
        "result_findings": [],
        "issues": [],
        "uncertainty": raw.get("uncertainty")
        if isinstance(raw.get("uncertainty"), str) and raw["uncertainty"].strip()
        else _UNCERTAINTY,
    }
    try:
        supplied = FindingCorrespondence.model_validate(
            {key: raw.get(key) for key in FindingCorrespondence.model_fields}
        )
    except ValidationError:
        row["issues"].append("The supplied correspondence has malformed IDs, relation, or basis.")
        return row
    left, right = list(supplied.baseline_snapshot_ids), list(supplied.result_snapshot_ids)
    row.update(baseline_snapshot_ids=left, result_snapshot_ids=right)
    if not supplied.basis.strip():
        row["issues"].append("The supplied correspondence has no explanation of its basis.")
    if len(set(left)) != len(left) or len(set(right)) != len(right):
        row["issues"].append("The supplied correspondence repeats a finding snapshot ID.")
    for ids, findings, side in ((left, before, "baseline"), (right, after, "result")):
        for finding_id in ids:
            if finding_id not in findings:
                row["issues"].append(
                    f"Finding {finding_id} is missing from the exact saved {side} run."
                )
    valid_shape = {
        "continuation": len(left) == len(right) == 1,
        "removed": bool(left) and not right,
        "added": not left and bool(right),
        "split": len(left) == 1 and len(right) > 1,
        "merge": len(left) > 1 and len(right) == 1,
        "uncertain": bool(left) and bool(right),
    }[supplied.relation]
    if not valid_shape:
        row["issues"].append("The supplied relation does not match its declared finding counts.")
    if not row["issues"]:
        row.update(
            status="valid",
            baseline_findings=[before[value] for value in left],
            result_findings=[after[value] for value in right],
        )
    return row


def project_saved_comparison(
    bundle: EvidenceBundle | Mapping[str, Any],
    scenario_id: str | None,
    *,
    imported: bool = False,
    locally_computed: bool | None = False,
) -> dict:
    """Project exact saved references; local job execution remains a separate view."""
    data = (
        bundle.model_dump(mode="json") if isinstance(bundle, EvidenceBundle) else deepcopy(bundle)
    )
    if locally_computed:
        label, origin = "Locally computed Swarm analysis", "locally_computed"
        execution_limit = "This is a read-only view of the recorded local result."
    elif locally_computed is None:
        label, origin = "Saved Swarm analysis", "saved_native"
        execution_limit = "The saved request does not establish where this result was computed."
    else:
        label, origin = (
            ("Saved imported analysis", "imported_saved")
            if imported
            else ("Saved analysis", "saved_native")
        )
        execution_limit = (
            "This view reads supplied saved analysis; it neither creates nor proves a local "
            "execution job and does not recompute results."
        )
    projection = {
        "case_id": data.get("case", {}).get("id"),
        "scenario_id": scenario_id,
        "status": "not_selected",
        "summary": "Choose a saved scenario to inspect its recorded comparison.",
        "label": label,
        "origin": origin,
        "scenario_status": None,
        "baseline_run_id": None,
        "result_run_id": None,
        "baseline_run": None,
        "result_run": None,
        "result_available": False,
        "baseline_findings": [],
        "result_findings": [],
        "correspondences": [],
        "issues": [],
        "excluded_record_ids": [],
        "reason": "",
        "limitations": [
            execution_limit,
            _UNCERTAINTY,
            "Relationships come only from this exact scenario. No correspondence is inferred "
            "for review-version aliases or from matching exclusions or run IDs.",
            "Exact source references establish traceability, not factual truth or source "
            "independence. Local media availability is checked by the existing inspector.",
        ],
    }

    def finish(status: str, summary: str) -> dict:
        return deepcopy({**projection, "status": status, "summary": summary})

    if not scenario_id:
        return projection
    scenario = next(
        (row for row in data.get("scenarios", []) if row.get("id") == scenario_id), None
    )
    if scenario is None:
        return finish("not_saved", "This selection has no exact saved native comparison scenario.")
    projection.update(
        scenario_status=scenario.get("status", "dependency_preview"),
        baseline_run_id=scenario.get("base_run_id"),
        result_run_id=scenario.get("result_run_id"),
        excluded_record_ids=scenario.get("excluded_record_ids", []),
        reason=scenario.get("reason", ""),
    )
    runs = {row["id"]: row for row in data.get("runs", [])}
    baseline_id, result_id = projection["baseline_run_id"], projection["result_run_id"]
    if not isinstance(baseline_id, str) or (
        result_id is not None and not isinstance(result_id, str)
    ):
        projection["issues"].append("The saved scenario has a malformed analysis run reference.")
        return finish(
            "invalid", "Saved comparison references are malformed; no result is inferred."
        )
    baseline, result = runs.get(baseline_id), runs.get(result_id)
    projection.update(baseline_run=_run_summary(baseline), result_run=_run_summary(result))
    if baseline:
        projection["baseline_findings"] = [
            _finding(data, row)
            for row in data.get("findings", [])
            if row.get("run_id") == baseline_id
        ]
    status = projection["scenario_status"]
    if status == "dependency_preview":
        return finish(
            "preview", "Saved dependency preview only; no completed comparison is claimed."
        )
    if status == "failed":
        return finish(
            "failed", "The saved scenario reports failure; no completed result is claimed."
        )
    if status != "completed":
        return finish("incomplete", "The saved scenario is not marked as a completed comparison.")
    if result is None:
        return finish(
            "missing_result", "The exact completed result run is not available in this case."
        )
    if result_id == baseline_id:
        projection["issues"].append("The saved baseline and result run references are identical.")
        return finish("invalid", "A distinct saved result run was not supplied.")
    if result.get("status", "completed") != "completed":
        return finish(
            "failed" if result.get("status") == "failed" else "incomplete",
            "The saved result run is not marked completed; no completed result is claimed.",
        )
    projection["result_available"] = True
    projection["result_findings"] = [
        _finding(data, row) for row in data.get("findings", []) if row.get("run_id") == result_id
    ]
    if baseline is None:
        projection["issues"].append("The exact saved baseline run is missing.")
        return finish("invalid", "The saved result can be inspected, but its baseline is missing.")
    before = {row["snapshot_id"]: row for row in projection["baseline_findings"]}
    after = {row["snapshot_id"]: row for row in projection["result_findings"]}
    supplied = scenario.get("correspondences")
    if supplied is None or supplied == []:
        return finish(
            "missing_correspondences",
            "The result run is saved, but this exact scenario contains no finding correspondences. "
            "Relationships are not inferred from other scenarios.",
        )
    if not isinstance(supplied, list):
        return finish("invalid", "The saved correspondence collection is malformed.")
    rows = [_correspondence(row, index, before, after) for index, row in enumerate(supplied)]
    projection["correspondences"] = rows
    for row in rows:
        projection["issues"].extend(row["issues"])
    for finding in (*before.values(), *after.values()):
        projection["issues"].extend(finding["issues"])
    invalid = sum(row["status"] != "valid" for row in rows)
    if invalid:
        return finish(
            "invalid" if invalid == len(rows) else "partial",
            "Some supplied relationships cannot be resolved; inspect the valid saved references "
            "and listed gaps without inferring missing links.",
        )
    if projection["issues"]:
        return finish("partial", "Saved relationships resolve, but some evidence needs inspection.")
    return finish(
        "ready", "Exact saved baseline/result relationships are available for inspection."
    )
