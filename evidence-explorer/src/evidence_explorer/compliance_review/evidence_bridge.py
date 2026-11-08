"""Read-only projection of explicitly selected saved findings for review.

This bridge does not classify content, infer coverage, run Swarm, or record a
decision. It joins only links in the validated evidence bundle; matching text,
display names, and timing never create cross-record associations.
"""

from __future__ import annotations

from collections import defaultdict
from typing import Any

from ..review.contracts import EvidenceBundle


def project_saved_bundle(bundle: EvidenceBundle | dict, *, run_ids: list[str]) -> dict[str, Any]:
    """Project baseline evidence and selected run findings into matrix inputs.

    The caller must choose exact runs, including an empty list when only source
    records are wanted. This baseline-only view never transfers reviews from a
    scenario. Original exact fragments, run IDs, and record hashes remain in
    the output. Findings without a record-level citation remain visible in
    ``unlinked_findings``. No coverage declaration is manufactured.
    """
    bundle = EvidenceBundle.model_validate(
        bundle.model_dump(mode="json") if isinstance(bundle, EvidenceBundle) else bundle
    )
    runs = {run.id: run for run in bundle.runs}
    if len(set(run_ids)) != len(run_ids) or any(run_id not in runs for run_id in run_ids):
        raise ValueError("Select distinct run IDs from this saved case")
    selected_runs = set(run_ids)
    fragments = {fragment.id: fragment for fragment in bundle.fragments}
    findings = {
        finding.snapshot_id: finding
        for finding in bundle.findings
        if finding.run_id in selected_runs
    }
    reviews: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for review in bundle.reviews:
        finding = findings.get(review.finding_snapshot_id)
        if finding and review.run_id == finding.run_id and review.scenario_id is None:
            reviews[finding.snapshot_id].append(review.model_dump(mode="json"))

    associations: dict[str, dict[str, list[dict[str, Any]]]] = defaultdict(
        lambda: defaultdict(list)
    )
    for link in bundle.links:
        if link.target.type != "finding" or link.target.id not in findings:
            continue
        if link.relation not in {"supports", "contradicts"}:
            continue
        finding = findings[link.target.id]
        if link.run_id is not None and link.run_id != finding.run_id:
            continue
        # Only explicit record/fragment references associate a finding with a
        # record; an ungrounded inference-to-inference link cannot do so.
        record_ids = set()
        cited_ids = set(link.fragment_ids)
        if link.source.type == "record":
            record_ids.add(link.source.id)
        elif link.source.type == "fragment":
            cited_ids.add(link.source.id)
        for fragment_id in cited_ids:
            record_ids.add(fragments[fragment_id].record_id)
        for record_id in record_ids:
            related = [
                fragments[fragment_id].model_dump(mode="json")
                for fragment_id in sorted(cited_ids)
                if fragments[fragment_id].record_id == record_id
            ]
            associations[record_id][finding.snapshot_id].append(
                {
                    "link_id": link.id,
                    "relation": link.relation,
                    "basis": link.basis,
                    "fragments": related,
                }
            )

    def context(finding_id: str, evidence: list[dict[str, Any]]) -> dict[str, Any]:
        finding = findings[finding_id]
        run = runs[finding.run_id]
        swarm = finding.details.get("domain") == "swarm" or run.method.startswith("swarm_detector.")
        return {
            "finding_id": finding.snapshot_id,
            "finding_version": finding.version,
            "run_id": finding.run_id,
            "kind": finding.kind,
            "statement": finding.statement,
            "details": finding.details,
            "method": run.method,
            "method_version": run.method_version,
            "input_fingerprint": run.input_fingerprint,
            "configuration": run.configuration,
            "producer_reports_full_inputs": run.full_inputs_available,
            "recomputation_capability": run.recomputation_capability,
            "run_coverage": run.coverage,
            "status": run.status,
            "evidence_links": evidence,
            "baseline_reviews": reviews[finding_id],
            "type": "swarm" if swarm else "analysis",
        }

    rows = []
    linked = set()
    for record in bundle.records:
        if record.text is None:
            continue
        by_finding = associations[record.id]
        linked.update(by_finding)
        contexts = [context(fid, by_finding[fid]) for fid in sorted(by_finding)]
        rows.append(
            {
                "id": record.id,
                "kind": record.kind,
                "text": record.text,
                "published_at": record.published_at,
                "acquired_at": record.acquired_at,
                "source_label": record.source_label,
                "source_url": record.source_url,
                "content_hash": record.content_hash,
                "source_metadata": record.metadata,
                "analysis": [item for item in contexts if item["type"] == "analysis"],
                "swarm": [item for item in contexts if item["type"] == "swarm"],
            }
        )
    return {
        "case_id": bundle.case.id,
        "synthetic": bundle.case.synthetic,
        "case_coverage": bundle.case.coverage,
        "case_limitations": bundle.case.limitations,
        "selected_run_ids": list(run_ids),
        "records": rows,
        "unlinked_findings": [context(fid, []) for fid in sorted(set(findings) - linked)],
        "limitations": [
            "Saved findings are context for review, not category decisions or proof "
            "of enforcement.",
            "No monthly coverage, identity correspondence, or new human decision is inferred.",
            "Run input availability is the producer's declaration, not independent verification.",
            "Artifact-only records are not text classification inputs.",
        ],
    }
