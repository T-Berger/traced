"""Link category review rows to explicitly selected saved Swarm observations.

This is a read-only projection, not a new analysis or finding correspondence.
The caller must capture the assessment view and bundle in one workspace snapshot.
"""

from __future__ import annotations

from copy import deepcopy
from typing import Any

from ..review.contracts import EvidenceBundle
from .evidence_bridge import project_saved_bundle
from .review_projection import project_category_review


def project_category_swarm_context(
    view: dict[str, Any],
    bundle: EvidenceBundle | dict,
    *,
    record_kinds: list[str],
    target: str,
    swarm_run_ids: list[str],
) -> dict[str, Any]:
    """Attach explicit Swarm context through the identical saved record only.

    Existing category decisions stay on their own exact finding and assessment.
    Swarm observations retain the originating run and evidence links. Neither a
    shared record nor a behavioral pattern establishes a category, violation,
    automation, intent, or independent corroboration.
    """
    bundle = EvidenceBundle.model_validate(
        bundle.model_dump(mode="json") if isinstance(bundle, EvidenceBundle) else bundle
    )
    if not isinstance(swarm_run_ids, list) or any(
        not isinstance(run_id, str) or not run_id for run_id in swarm_run_ids
    ):
        raise ValueError("Choose explicit saved Swarm run identifiers.")
    runs = {run.id: run for run in bundle.runs}
    if len(set(swarm_run_ids)) != len(swarm_run_ids) or any(
        run_id not in runs or not runs[run_id].method.startswith("swarm_detector.")
        for run_id in swarm_run_ids
    ):
        raise ValueError("Select distinct saved Swarm detector runs from this case.")

    category = project_category_review(view, bundle, record_kinds=record_kinds, target=target)
    source = project_saved_bundle(bundle, run_ids=swarm_run_ids)
    by_record = {record["id"]: record for record in source["records"]}
    result = deepcopy(category)
    for row in result["rows"]:
        saved = by_record.get(row["record_id"])
        if not saved or saved["content_hash"] != row["record_content_hash"]:
            raise ValueError("The shared saved record changed; reload before comparing.")
        row["swarm_context"] = deepcopy(saved["swarm"])
    result["selected_swarm_run_ids"] = list(swarm_run_ids)
    result["unlinked_swarm_findings"] = deepcopy(source["unlinked_findings"])
    result["limitations"] += [
        "Swarm observations are separate baseline snapshots and do not inherit category decisions.",
        "Only an explicitly linked identical saved record connects the two methods.",
        "Coordination is not evidence of automation, intent, a violation, or an operator.",
    ]
    return result
