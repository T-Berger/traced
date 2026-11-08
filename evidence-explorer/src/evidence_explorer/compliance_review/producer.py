"""Build immutable, source-bound category proposal findings for shared review."""

from datetime import UTC, datetime
from uuid import uuid4

from ..review.contracts import EvidenceBundle
from .matrix import CATEGORIES
from .proposals import PROMPT_VERSION


def category_proposal_bundle(
    bundle: EvidenceBundle, record_id: str, result: dict
) -> EvidenceBundle:
    """Append one model run to a detached bundle; never attach a human decision."""
    bundle = EvidenceBundle.model_validate(bundle.model_dump(mode="json"))
    record = next((item for item in bundle.records if item.id == record_id), None)
    if record is None or record.text is None:
        raise ValueError("The selected saved text is unavailable.")
    if (
        result.get("record_id") != record_id
        or result.get("record_content_hash") != record.content_hash
    ):
        raise ValueError("The proposal is not bound to the selected saved record version.")
    target, run_id, model = result.get("target"), result.get("run_id"), result.get("model")
    if not all(isinstance(v, str) and v.strip() for v in (target, run_id, model)):
        raise ValueError("Proposal target, run and model provenance are required.")
    if result.get("provider") != "ollama" or result.get("method") != PROMPT_VERSION:
        raise ValueError("Unexpected model proposal method or provider.")
    annotations = result.get("annotations")
    if not isinstance(annotations, list):
        raise ValueError("A proposal candidate list is required.")
    payload = bundle.model_dump(mode="json")
    payload["runs"].append(
        {
            "id": run_id,
            "input_fingerprint": bundle.fingerprint(),
            "input_manifest": [record_id],
            "method": "compliance_review.proposals.propose_for_record",
            "method_version": PROMPT_VERSION,
            "created_at": datetime.now(UTC).isoformat(),
            "full_inputs_available": False,
            "recomputation_capability": "dependency_review_only",
            "configuration": {
                "provider": "ollama",
                "model": model,
                "target": target,
                "record_content_hash": record.content_hash,
                "uncertainty": result.get("uncertainty"),
                "rejected_candidates": result.get("rejected_candidates", []),
                "generation_metadata": result.get("generation_metadata", {}),
            },
            "coverage": {"status": "one saved record; no population coverage asserted"},
        }
    )
    for index, annotation in enumerate(annotations):
        if not isinstance(annotation, dict):
            raise ValueError("Invalid category proposal.")
        category, quote = annotation.get("category"), annotation.get("quote")
        start, end = annotation.get("start"), annotation.get("end")
        rationale = annotation.get("rationale")
        if (
            category not in CATEGORIES
            or annotation.get("status") != "proposed"
            or annotation.get("origin") != "model"
            or annotation.get("record_id") != record_id
            or annotation.get("record_content_hash") != record.content_hash
            or annotation.get("run_id") != run_id
            or annotation.get("target") != target
            or annotation.get("model") != model
            or type(start) is not int
            or type(end) is not int
            or not isinstance(quote, str)
            or not 0 <= start < end <= len(record.text)
            or record.text[start:end] != quote
            or not isinstance(rationale, str)
            or not rationale.strip()
        ):
            raise ValueError("Category proposal has invalid provenance or exact evidence.")
        stem = f"{run_id}:{index}"
        fragment_id, finding_id = f"{stem}:fragment", f"{stem}:finding"
        payload["fragments"].append(
            {
                "id": fragment_id,
                "record_id": record_id,
                "selector": {"kind": "text", "start": start, "end": end},
                "quote": quote,
            }
        )
        payload["findings"].append(
            {
                "snapshot_id": finding_id,
                "lineage_id": finding_id,
                "version": 1,
                "run_id": run_id,
                "kind": "inference",
                "statement": f"Model proposal: {category.replace('_', ' ')} — {rationale}",
                "details": {
                    "domain": "compliance_category",
                    "category": category,
                    "target": target,
                    "origin": "model",
                    "model": model,
                    "record_id": record_id,
                    "record_content_hash": record.content_hash,
                },
            }
        )
        payload["links"].append(
            {
                "id": f"{stem}:link",
                "source": {"type": "fragment", "id": fragment_id},
                "target": {"type": "finding", "id": finding_id},
                "relation": "supports",
                "fragment_ids": [fragment_id],
                "basis": "Exact saved text cited by model proposal",
                "run_id": run_id,
            }
        )
    return EvidenceBundle.model_validate(payload)


def category_batch_bundle(bundle: EvidenceBundle, proposals: list[dict]) -> EvidenceBundle:
    """Combine separately generated, validated records into one bounded review run."""
    bundle = EvidenceBundle.model_validate(bundle.model_dump(mode="json"))
    if not isinstance(proposals, list) or not 1 <= len(proposals) <= 20:
        raise ValueError("A batch requires 1 to 20 completed record proposals.")
    ids = [p.get("record_id") for p in proposals if isinstance(p, dict)]
    if len(ids) != len(proposals) or len(ids) != len(set(ids)):
        raise ValueError("Batch record identities must be distinct.")
    if (
        len({p.get("target") for p in proposals}) != 1
        or len({p.get("model") for p in proposals}) != 1
    ):
        raise ValueError("A batch must use one declared target and model identity.")
    payload = bundle.model_dump(mode="json")
    batch_id = f"category-batch:{uuid4()}"
    metadata = []
    batch_run = None
    for proposal in proposals:
        checked = category_proposal_bundle(bundle, proposal["record_id"], proposal).model_dump(
            mode="json"
        )
        run = checked["runs"][-1]
        metadata.append(
            {
                "record_id": proposal["record_id"],
                "record_content_hash": proposal["record_content_hash"],
                "uncertainty": proposal.get("uncertainty"),
                "rejected_candidates": proposal.get("rejected_candidates", []),
                "generation_metadata": proposal.get("generation_metadata", {}),
            }
        )
        if batch_run is None:
            batch_run = run
        for finding in checked["findings"][len(bundle.findings) :]:
            finding["run_id"] = batch_id
            payload["findings"].append(finding)
        for link in checked["links"][len(bundle.links) :]:
            link["run_id"] = batch_id
            payload["links"].append(link)
        payload["fragments"].extend(checked["fragments"][len(bundle.fragments) :])
    batch_run["id"] = batch_id
    batch_run["input_manifest"] = ids
    for field in (
        "record_content_hash",
        "uncertainty",
        "rejected_candidates",
        "generation_metadata",
    ):
        batch_run["configuration"].pop(field, None)
    batch_run["configuration"]["record_results"] = metadata
    batch_run["coverage"] = {
        "status": "selected saved records only; no population coverage asserted"
    }
    payload["runs"].append(batch_run)
    return EvidenceBundle.model_validate(payload)


def alerts_proposal_bundle(bundle: EvidenceBundle, projection: dict, target: str) -> EvidenceBundle:
    """Convert a checked Alerts projection into a separate, source-linked analysis run.

    The caller must reconstruct the projection from the selected analytics
    snapshot and commit under a matching case fingerprint. The newly assigned
    run identifies this import, not an unknown upstream classifier run.
    """
    bundle = EvidenceBundle.model_validate(bundle.model_dump(mode="json"))
    if projection.get("case_id") != bundle.case.id:
        raise ValueError("Alerts projection belongs to a different case.")
    if not isinstance(target, str) or not target.strip() or len(target) > 500:
        raise ValueError("Enter an explicit target of at most 500 characters.")
    annotations = projection.get("annotations")
    if not isinstance(annotations, list) or not annotations:
        raise ValueError("No source-linked Alerts proposals are available to save.")
    records = {record.id: record for record in bundle.records}
    record_ids = []
    for item in annotations:
        if not isinstance(item, dict):
            raise ValueError("Invalid Alerts proposal.")
        record = records.get(item.get("record_id"))
        start, end, quote = item.get("start"), item.get("end"), item.get("quote")
        if (
            record is None
            or record.text is None
            or item.get("category") not in CATEGORIES
            or item.get("origin") != "model"
            or item.get("status") != "proposed"
            or item.get("record_content_hash") != record.content_hash
            or type(start) is not int
            or type(end) is not int
            or not isinstance(quote, str)
            or not 0 <= start < end <= len(record.text)
            or record.text[start:end] != quote
            or not isinstance(item.get("model"), str)
            or not item["model"].strip()
            or not isinstance(item.get("method"), str)
            or not item["method"].startswith("alerts:")
            or not item.get("analytics_case_id")
            or item.get("analytics_case_id") != projection.get("analytics_case_id")
            or not item.get("analytics_item_id")
            or not item.get("capture_sha256")
        ):
            raise ValueError("Alerts proposal lacks exact source or classifier provenance.")
        record_ids.append(record.id)
    payload = bundle.model_dump(mode="json")
    run_id = f"alerts-import:{uuid4()}"
    payload["runs"].append(
        {
            "id": run_id,
            "input_fingerprint": bundle.fingerprint(),
            "input_manifest": list(dict.fromkeys(record_ids)),
            "method": "compliance_review.alerts_bridge.project_alert_snapshot",
            "method_version": "1",
            "created_at": datetime.now(UTC).isoformat(),
            "full_inputs_available": False,
            "recomputation_capability": "dependency_review_only",
            "configuration": {
                "target": target.strip(),
                "analytics_case_id": projection.get("analytics_case_id"),
                "limitations": projection.get("limitations", []),
            },
            "coverage": {"status": "selected mapped records only; no population coverage asserted"},
        }
    )
    for index, item in enumerate(annotations):
        stem = f"{run_id}:{index}"
        fragment_id, finding_id = f"{stem}:fragment", f"{stem}:finding"
        payload["fragments"].append(
            {
                "id": fragment_id,
                "record_id": item["record_id"],
                "selector": {"kind": "text", "start": item["start"], "end": item["end"]},
                "quote": item["quote"],
            }
        )
        rationale = item.get("rationale")
        suffix = f" — {rationale}" if isinstance(rationale, str) and rationale.strip() else ""
        payload["findings"].append(
            {
                "snapshot_id": finding_id,
                "lineage_id": finding_id,
                "version": 1,
                "run_id": run_id,
                "kind": "inference",
                "statement": (
                    f"Alerts classifier proposal: {item['category'].replace('_', ' ')}{suffix}"
                ),
                "details": {
                    "domain": "compliance_category",
                    "category": item["category"],
                    "target": target.strip(),
                    "origin": "model",
                    "model": item["model"],
                    "record_id": item["record_id"],
                    "record_content_hash": item["record_content_hash"],
                    "alerts_provenance": {
                        key: item.get(key)
                        for key in (
                            "method",
                            "score",
                            "analytics_case_id",
                            "analytics_item_id",
                            "capture_sha256",
                            "context_records",
                            "interpretation",
                        )
                    },
                },
            }
        )
        payload["links"].append(
            {
                "id": f"{stem}:link",
                "source": {"type": "fragment", "id": fragment_id},
                "target": {"type": "finding", "id": finding_id},
                "relation": "supports",
                "fragment_ids": [fragment_id],
                "basis": "Saved text supplied as classifier context",
                "run_id": run_id,
            }
        )
    return EvidenceBundle.model_validate(payload)
