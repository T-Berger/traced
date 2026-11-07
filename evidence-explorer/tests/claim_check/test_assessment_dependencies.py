"""Counterevidence remains inspectable and exclusions invalidate its assessment."""

import hashlib

import pytest

from evidence_explorer.claim_check.bundles import build_result_bundle
from evidence_explorer.claim_check.contracts import (
    AssessmentCitation,
    CheckPolicy,
    ClaimCheckJob,
    ClaimCheckPayload,
    EvidenceFragment,
    EvidenceSnapshot,
    ProposedAssessment,
    SavedEvidence,
)
from evidence_explorer.review.analysis import compute_impact
from evidence_explorer.review.contracts import EvidenceBundle, Finding
from evidence_explorer.review.workflow import _citations

NOW = "2026-11-07T00:00:00+00:00"


@pytest.fixture
def checked_bundle():
    records, fragments = [], []
    for key, text in (
        ("contradiction", "The launch occurred on 6 November, not 5 November."),
        ("context", "The schedule listed 5 November as provisional."),
        ("challenge", "The launch record refers to the second launch attempt."),
    ):
        records.append(
            SavedEvidence(
                id=key,
                url=f"https://example.org/{key}",
                requested_url=f"https://example.org/{key}",
                title=key,
                publisher="Example",
                text=text,
                content_hash=hashlib.sha256(text.encode()).hexdigest(),
                retrieved_at=NOW,
            )
        )
        fragments.append(
            EvidenceFragment(id=f"f-{key}", record_id=key, text=text, start=0, end=len(text))
        )
    snapshot = EvidenceSnapshot(records=records, fragments=fragments, created_at=NOW)
    finding = Finding(
        snapshot_id="source-finding",
        lineage_id="launch-date",
        version=1,
        run_id="source-run",
        kind="inference",
        statement="The launch occurred on 5 November.",
    )
    job = ClaimCheckJob(
        id="dependency-test",
        source_case_id="source-case",
        source_finding_id=finding.snapshot_id,
        status="running",
        stage="assessing_claim",
        payload=ClaimCheckPayload(
            source_case_id="source-case",
            source_finding_id=finding.snapshot_id,
            source_case_title="Launch date",
            source_case_fingerprint="source-version",
            finding_snapshot=finding,
            policy=CheckPolicy(),
            requested_model="test-model",
        ),
        created_at=NOW,
        updated_at=NOW,
    )
    assessment = ProposedAssessment(
        status="contradicted",
        explanation="The recorded launch date differs from the provisional date.",
        citations=[
            AssessmentCitation(
                fragment_id=fragments[0].id, quote=fragments[0].text, stance="contradicts"
            ),
            AssessmentCitation(
                fragment_id=fragments[1].id, quote=fragments[1].text, stance="context"
            ),
        ],
        metadata={
            "challenge": {
                "status": "completed",
                "findings": [
                    {
                        "finding_type": "alternative_explanation",
                        "explanation": "The date may refer to another attempt.",
                        "fragment_ids": [fragments[2].id],
                        "quotes": [fragments[2].text],
                    }
                ],
            }
        },
    )
    return build_result_bundle(job, snapshot, assessment)


@pytest.mark.parametrize("source", ["contradiction", "context", "challenge"])
def test_excluding_any_reviewed_source_marks_assessment_affected(checked_bundle, source):
    record_id = next(
        record.id for record in checked_bundle.records if record.source_label == source
    )
    impact = compute_impact(checked_bundle, [record_id])
    assert impact["affected_finding_ids"] == [checked_bundle.findings[0].snapshot_id]
    assert record_id not in impact["remaining_evidence"][checked_bundle.findings[0].snapshot_id]


def test_dependencies_never_turn_context_or_counterevidence_into_support(checked_bundle):
    assert not any(link.relation == "supports" for link in checked_bundle.links)
    assert any(link.relation == "contradicts" for link in checked_bundle.links)
    inputs = [link for link in checked_bundle.links if getattr(link, "assessment_input", False)]
    assert len(inputs) == 5
    assert sum(bool(getattr(link, "challenge_input", False)) for link in inputs) == 1


def test_brief_can_retrieve_contradictory_context_and_challenge_passages(checked_bundle):
    # Namespaced dependencies and quotes survive the portable schema boundary.
    reopened = EvidenceBundle.model_validate_json(checked_bundle.model_dump_json())
    citations = _citations(
        reopened.model_dump(mode="json"), reopened.findings[0].snapshot_id, set()
    )
    assert {citation["source_label"] for citation in citations} == {
        "contradiction",
        "context",
        "challenge",
    }
    quoted = {citation["quote"] for citation in citations}
    assert quoted == {record.text for record in reopened.records}
    assert len(citations) == 3
    assert {citation["source_label"]: citation["evidence_role"] for citation in citations} == {
        "contradiction": "contradicts",
        "context": "context",
        "challenge": "candidate_objection",
    }
    contradiction_record = next(
        record.id for record in reopened.records if record.source_label == "contradiction"
    )
    remaining = _citations(
        reopened.model_dump(mode="json"), reopened.findings[0].snapshot_id, {contradiction_record}
    )
    assert {citation["source_label"] for citation in remaining} == {"context", "challenge"}
