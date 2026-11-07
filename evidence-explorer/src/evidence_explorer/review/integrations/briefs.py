"""Readable briefing export from the same saved-decision projection as the UI."""

from __future__ import annotations

import json
from typing import Any

from ..origins import claim_origin_sentence


def render_brief(projection: dict[str, Any]) -> str:
    case, assessment = projection["case"], projection["assessment"]
    lines = [f"# {case['title']}", "", case.get("question", ""), ""]
    lines += [
        f"Assessment: **{assessment['label']}** · Version {assessment['version']}",
        f"Run: `{assessment['base_run_id']}`",
        "",
    ]
    if case.get("synthetic"):
        lines += [
            "**Selected case: Synthetic example.**\n"
            "This label applies to the selected case. Saved records retain their own "
            "provenance; collected sources are not labeled fictional.",
            "",
        ]
    lines += ["## Included statements", ""]
    if not projection["included"]:
        lines += ["No statements have a saved Use or Qualify decision for this assessment.", ""]
    for row in projection["included"]:
        review = row["review"]
        lines += [
            f"### {row['statement']}",
            "",
            f"Finding snapshot: `{row['finding_id']}` · {row['kind']}",
            *([claim_origin_sentence(row["claim_origin"])] if row.get("claim_origin") else []),
            f"Decision: **{row['disposition']}** · {review['reviewer']} · {review['created_at']}",
            "",
            ("Qualification: " if row["disposition"] == "qualify" else "Reviewer reason: ")
            + row["reason"],
            "",
            "Evidence references:",
            "",
        ]
        for citation in row["evidence"]:
            selector = citation["selector"]
            selected = (
                f"characters {selector['start']}–{selector['end']} (end exclusive)"
                if selector["kind"] == "text"
                else selector["kind"].replace("_", " ")
            )
            lines += [
                f"- `{citation['fragment_id'] or citation['record_id']}` — "
                f"{citation['source_label']} · record `{citation['record_id']}` · {selected}",
            ]
            provenance = citation.get("record_synthetic")
            origin = (
                "Synthetic record"
                if provenance is True
                else "Collected source; factual accuracy requires review"
                if provenance is False
                else "Record origin not specified"
            )
            lines.append(f"  Record provenance: {origin}.")
            role_labels = {
                "supports": "Supports the checked claim (model proposal)",
                "contradicts": "Contradicts the checked claim (model proposal)",
                "context": "Context for the checked claim (model proposal)",
                "candidate_objection": "Candidate objection from the second model pass",
                "assessment_input": "Assessment input; no supporting stance is implied",
            }
            roles = citation.get("evidence_roles", [citation.get("evidence_role")])
            for role in dict.fromkeys(roles):
                if evidence_role := role_labels.get(role):
                    lines.append(f"  Evidence role: {evidence_role}.")
            fragment_ids = citation.get("fragment_ids", [])
            if len(fragment_ids) > 1:
                lines.append(
                    "  Exact fragment references: "
                    + ", ".join(f"`{fragment_id}`" for fragment_id in fragment_ids)
                )
            if citation.get("quote"):
                lines += ["", "> " + citation["quote"].replace("\n", "\n> "), ""]
            if citation.get("artifact_ref"):
                lines.append(f"  Supplied artifact: `{citation['artifact_ref']}`")
            lines.append(
                "  Original public source: " + (citation.get("source_url") or "Not supplied")
            )
        if not row["evidence"]:
            lines.append("No remaining linked supporting evidence; inspect the stated assumption.")
        lines.append("")
    lines += ["## Omitted statements", ""]
    if not projection["omitted"]:
        lines += ["None.", ""]
    for row in projection["omitted"]:
        label = {
            "withheld": "Withheld",
            "needs_review": "Needs review",
            "not_reviewed": "Not reviewed",
        }[row["status"]]
        lines += [
            f"- **{label}: {row['statement']}**",
            f"  Finding snapshot: `{row['finding_id']}`",
            *(
                ["  " + claim_origin_sentence(row["claim_origin"])]
                if row.get("claim_origin")
                else []
            ),
            f"  {row['reason']}",
            "",
        ]
    lines += ["## Assessment scope", ""]
    if projection["excluded_record_ids"]:
        lines += ["Excluded records:", ""]
        lines += [f"- `{record_id}`" for record_id in projection["excluded_record_ids"]]
    else:
        lines.append("No supplied records excluded.")
    lines += [
        "",
        "Exclusion impact identifies dependencies. Detector or model results change only "
        "after a completed recomputation, stored separately in the case package.",
        "",
        "## Analysis methods",
        "",
    ]
    for run in projection["methods"]:
        lines += [
            f"- `{run['id']}`: {run['method']}, version {run['method_version']}",
            f"  - Input fingerprint: `{run['input_fingerprint']}`",
            "  - Configuration: `" + json.dumps(run.get("configuration", {}), sort_keys=True) + "`",
            "  - Coverage: `" + json.dumps(run.get("coverage", {}), sort_keys=True) + "`",
        ]
    lines += [
        "",
        "## Coverage and limitations",
        "",
        "Case coverage: `" + json.dumps(projection["coverage"], sort_keys=True) + "`",
        "",
        *["- " + limitation for limitation in projection["limitations"]],
        "",
        "The accompanying case package preserves exact evidence, immutable finding snapshots, "
        "all scenario versions, and review history. This brief uses only the selected assessment.",
        "",
    ]
    return "\n".join(lines)
