"""Adapt a completed claim check into an immutable, portable review case."""

from __future__ import annotations

from collections import defaultdict

from ..review.contracts import Case, EvidenceBundle, Finding, Fragment, Link, ObjectRef, Record, Run
from .assessor import validate_assessment
from .contracts import ClaimCheckJob, EvidenceSnapshot, ProposedAssessment


def build_result_bundle(
    job: ClaimCheckJob, snapshot: EvidenceSnapshot, assessment: ProposedAssessment
) -> EvidenceBundle:
    """Preserve the original claim and attach proposals separately from human decisions."""
    validate_assessment(assessment, snapshot)
    claim_synthetic = job.payload.claim_synthetic
    case_id, run_id, finding_id = (
        f"claim-check:{job.id}",
        f"claim-check:{job.id}:run",
        f"claim-check:{job.id}:finding",
    )
    prefix = f"claim-check:{job.id}:"
    record_ids = {record.id: prefix + record.id for record in snapshot.records}
    fragment_ids = {fragment.id: prefix + fragment.id for fragment in snapshot.fragments}
    records = [
        Record(
            id=record_ids[record.id],
            kind="article",
            source_label=record.title,
            text=record.text,
            content_hash=record.content_hash,
            source_url=record.url,
            acquired_at=record.retrieved_at,
            published_at=record.published_at,
            metadata={
                **record.metadata,
                "publisher": record.publisher,
                "requested_url": record.requested_url,
                "platform": "news",
                "independence": "not_established",
                "synthetic": False,
                "collection_record_id": record.id,
            },
        )
        for record in snapshot.records
    ]
    fragments = [
        Fragment(
            id=fragment_ids[fragment.id],
            record_id=record_ids[fragment.record_id],
            selector={"kind": "text", "start": fragment.start, "end": fragment.end},
            quote=fragment.text,
        )
        for fragment in snapshot.fragments
    ]
    origin_record_ids = {
        record.id: f"{prefix}origin:{record.id}" for record in job.payload.origin_records
    }
    for record in job.payload.origin_records:
        records.append(
            record.model_copy(
                update={
                    "id": origin_record_ids[record.id],
                    "metadata": {
                        **record.metadata,
                        "claim_origin": True,
                        "original_record_id": record.id,
                        "role": "Original claim context; not independent corroboration.",
                    },
                }
            )
        )
    for fragment in job.payload.origin_fragments:
        fragments.append(
            fragment.model_copy(
                update={
                    "id": f"{prefix}origin:{fragment.id}",
                    "record_id": origin_record_ids[fragment.record_id],
                }
            )
        )
    by_id = {fragment.id: fragment for fragment in snapshot.fragments}
    challenged_ids = {
        fragment_id
        for objection in (assessment.metadata.get("challenge") or {}).get("findings", [])
        for fragment_id in objection.get("fragment_ids", [])
    }
    # Dependency means this passage was an input to the assessment. It does not
    # mean the original claim is supported: even counterevidence and uncited
    # passages can change a proposed conclusion when excluded.
    links = [
        Link(
            id=f"{prefix}assessment-input:{index}",
            source=ObjectRef(type="fragment", id=fragment_ids[fragment.id]),
            target=ObjectRef(type="finding", id=finding_id),
            relation="depends_on",
            fragment_ids=[fragment_ids[fragment.id]],
            run_id=run_id,
            basis="Saved passage supplied to the assessment; input dependency does not "
            "establish support for the claim.",
            assessment_input=True,
            challenge_input=fragment.id in challenged_ids,
        )
        for index, fragment in enumerate(snapshot.fragments)
    ]
    citations = []
    for index, citation in enumerate(assessment.citations):
        source = by_id[citation.fragment_id]
        start = source.start + source.text.index(citation.quote)
        quote_id = f"{prefix}citation:{index}"
        fragments.append(
            Fragment(
                id=quote_id,
                record_id=record_ids[source.record_id],
                selector={"kind": "text", "start": start, "end": start + len(citation.quote)},
                quote=citation.quote,
            )
        )
        citations.append({**citation.model_dump(), "fragment_id": quote_id})
        # Retain factual stance separately from the input dependency. Briefs
        # deduplicate identical spans and prefer this explicit citation stance.
        if citation.stance != "context":
            links.append(
                Link(
                    id=f"{prefix}citation-link:{index}",
                    source=ObjectRef(type="fragment", id=quote_id),
                    target=ObjectRef(type="finding", id=finding_id),
                    relation=citation.stance,
                    fragment_ids=[quote_id],
                    run_id=run_id,
                    basis="Local model proposal; exact quotation checked; analyst review required.",
                    assessment_citation_stance=citation.stance,
                )
            )
        if citation.stance != "supports":
            links.append(
                Link(
                    id=f"{prefix}citation-dependency:{index}",
                    source=ObjectRef(type="fragment", id=quote_id),
                    target=ObjectRef(type="finding", id=finding_id),
                    relation="depends_on",
                    fragment_ids=[quote_id],
                    run_id=run_id,
                    basis="Cited assessment input; the declared stance is "
                    f"{citation.stance}, not support for the claim.",
                    assessment_input=True,
                    assessment_citation_stance=citation.stance,
                )
            )
    duplicates: dict[str, list[str]] = defaultdict(list)
    for record in snapshot.records:
        duplicates[record.content_hash].append(record_ids[record.id])
    duplicate_groups = [ids for ids in duplicates.values() if len(ids) > 1]
    for group_index, group in enumerate(duplicate_groups):
        for index, other in enumerate(group[1:]):
            links.append(
                Link(
                    id=f"{prefix}duplicate:{group_index}:{index}",
                    source=ObjectRef(type="record", id=group[0]),
                    target=ObjectRef(type="record", id=other),
                    relation="same_content",
                    basis=(
                        "Identical saved text hashes; origin and independence are not established."
                    ),
                    run_id=run_id,
                )
            )
    coverage = "empty" if not snapshot.records else ("partial" if snapshot.issues else "collected")
    limitations = list(
        dict.fromkeys(
            [
                *snapshot.limitations,
                *job.payload.origin_limitations,
                "The assessment covers selected saved passages from bounded external searches.",
                "Different URLs or publishers do not establish independent corroboration.",
                "Exact quotations validate references, not factual truth or semantic support.",
                "Coordination, deceptive intent and operator attribution were not assessed.",
                *[issue.message for issue in snapshot.issues],
            ]
        )
    )
    assessment_data = {**assessment.model_dump(mode="json"), "citations": citations}
    metadata = dict(assessment_data.get("metadata", {}))
    if "reviewed_fragment_ids" in metadata:
        metadata["reviewed_fragment_ids"] = [
            fragment_ids[identifier]
            for identifier in metadata["reviewed_fragment_ids"]
            if identifier in fragment_ids
        ]
    metadata["collection_to_export_fragment_ids"] = fragment_ids
    if metadata.get("challenge"):
        challenge = dict(metadata["challenge"])
        challenge["findings"] = [
            {**finding, "fragment_ids": [fragment_ids[fid] for fid in finding["fragment_ids"]]}
            for finding in challenge.get("findings", [])
        ]
        metadata["challenge"] = challenge
    assessment_data["metadata"] = metadata
    check_data = {
        "job_id": job.id,
        "source_case_id": job.source_case_id,
        "source_finding_id": job.source_finding_id,
        "source_case_fingerprint": job.payload.source_case_fingerprint,
        "source_synthetic": claim_synthetic,
        "source_case_synthetic": job.payload.case_synthetic,
        "source_synthetic_basis": (
            "finding_metadata" if claim_synthetic is not None else "unspecified"
        ),
        "source_finding_snapshot": job.payload.finding_snapshot.model_dump(mode="json"),
        "origin_record_ids": list(origin_record_ids.values()),
        "origin_limitations": job.payload.origin_limitations,
        "snapshot_fingerprint": snapshot.fingerprint(),
        "search_batches": [batch.model_dump(mode="json") for batch in snapshot.searches],
        "issues": [issue.model_dump(mode="json") for issue in snapshot.issues],
        "collection_coverage": coverage,
        "attempted_urls": snapshot.attempted_urls,
        "exact_duplicate_groups": duplicate_groups,
        "independence": "not_established",
        "coordination": "not_assessed",
        "deceptive_intent": "not_established",
        "review_status": "awaiting_review",
    }
    return EvidenceBundle(
        case=Case(
            id=case_id,
            title=f"Claim check · {job.payload.claim[:90]}",
            question=job.payload.claim,
            synthetic=claim_synthetic is True,
            coverage={
                "collection": coverage,
                "saved_sources": len(snapshot.records),
                "original_context_records": len(origin_record_ids),
                "exhaustive": False,
            },
            limitations=limitations,
        ),
        records=records,
        fragments=fragments,
        runs=[
            Run(
                id=run_id,
                input_fingerprint=snapshot.fingerprint(),
                input_manifest=[record.id for record in records],
                method="external_claim_check",
                method_version="1",
                created_at=snapshot.created_at,
                full_inputs_available=True,
                configuration={
                    "policy": job.payload.policy.model_dump(mode="json"),
                    "model": assessment.model,
                    "provider": assessment.provider,
                    "prompt_version": assessment.prompt_version,
                    "retrieval_version": snapshot.retrieval_version,
                },
                coverage={"collection": coverage, "limitations": limitations},
            )
        ],
        findings=[
            Finding(
                snapshot_id=finding_id,
                lineage_id=job.payload.finding_snapshot.lineage_id,
                version=job.payload.finding_snapshot.version,
                run_id=run_id,
                kind="inference",
                statement=job.payload.claim,
                details={
                    "domain": "claim_check",
                    **({"synthetic": claim_synthetic} if claim_synthetic is not None else {}),
                    "assessment": assessment_data,
                    "claim_check": check_data,
                },
            )
        ],
        links=links,
    )
