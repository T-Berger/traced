"""Claim provenance comes from its pinned finding, never its surrounding case."""

import hashlib
import json

import pytest

from evidence_explorer.claim_check.bundles import build_result_bundle
from evidence_explorer.claim_check.contracts import (
    ClaimCheckPayload,
    ClaimCheckResult,
    EvidenceSnapshot,
    ProposedAssessment,
)
from evidence_explorer.claim_check.service import ClaimCheckService
from evidence_explorer.claim_check.storage import StaleClaimCheck
from evidence_explorer.demo import seed_demo
from evidence_explorer.review.contracts import EvidenceBundle
from evidence_explorer.review.integrations.explorer import bundle_from_repository
from evidence_explorer.review.integrations.workspace import WorkspaceRepository

NOW = "2026-11-07T00:00:00+00:00"
MISSING = object()


def source_bundle(case_synthetic, finding_origin=MISSING, *, record_origins=None):
    origins = [True, False, MISSING] if record_origins is None else record_origins
    records, fragments, links = [], [], []
    for index, origin in enumerate(origins):
        identifier = f"source-record-{index}"
        text = f"Isolated test record {index}: the selected assertion retains its source context."
        records.append(
            {
                "id": identifier,
                "kind": "text",
                "source_label": identifier,
                "text": text,
                "content_hash": hashlib.sha256(text.encode()).hexdigest(),
                "metadata": {} if origin is MISSING else {"synthetic": origin},
            }
        )
        fragment_id = f"{identifier}:fragment"
        fragments.append(
            {
                "id": fragment_id,
                "record_id": identifier,
                "selector": {"kind": "text", "start": 0, "end": len(text)},
                "quote": text,
            }
        )
        links.append(
            {
                "id": f"source-link-{index}",
                "source": {"type": "fragment", "id": fragment_id},
                "target": {"type": "finding", "id": "source-finding"},
                "relation": "depends_on",
                "fragment_ids": [fragment_id],
                "basis": "Isolated origin-test source context.",
            }
        )
    return EvidenceBundle.model_validate(
        {
            "case": {
                "id": "source-case",
                "title": "Origin test case",
                "question": "Which provenance belongs to the selected claim?",
                "synthetic": case_synthetic,
            },
            "records": records,
            "fragments": fragments,
            "runs": [
                {
                    "id": "source-run",
                    "input_fingerprint": "origin-fixture",
                    "input_manifest": [record["id"] for record in records],
                    "method": "offline_test",
                    "method_version": "1",
                    "created_at": NOW,
                }
            ],
            "findings": [
                {
                    "snapshot_id": "source-finding",
                    "lineage_id": "claim-origin",
                    "version": 1,
                    "run_id": "source-run",
                    "kind": "inference",
                    "statement": "A selected test assertion with individually declared provenance.",
                    "details": {} if finding_origin is MISSING else {"synthetic": finding_origin},
                }
            ],
            "links": links,
        }
    )


def make_service(settings, bundle):
    repository = WorkspaceRepository(settings.db_path)
    repository.initialize()
    repository.import_bundle(bundle)
    return ClaimCheckService(repository, settings)


def submit(service, bundle):
    return service.submit(bundle.case.id, bundle.findings[0].snapshot_id)


def emit(job):
    snapshot = EvidenceSnapshot(created_at=NOW)
    assessment = ProposedAssessment(
        status="insufficient_evidence",
        explanation="Offline provenance test; no external evidence or model was requested.",
        provider="rules",
    )
    bundle = build_result_bundle(job, snapshot, assessment)
    return EvidenceBundle.model_validate_json(bundle.model_dump_json())


def assert_origin(bundle, expected, case_origin):
    finding = bundle.findings[0]
    check = finding.details["claim_check"]
    assert check["source_synthetic"] is expected
    assert check["source_case_synthetic"] is case_origin
    assert check["source_synthetic_basis"] == (
        "finding_metadata" if expected is not None else "unspecified"
    )
    assert bundle.case.synthetic is (expected is True)
    if expected is None:
        assert "synthetic" not in finding.details
    else:
        assert finding.details["synthetic"] is expected


def test_actual_explorer_mixed_case_preserves_selected_sample_claim_origin(repo, article, settings):
    seed_demo(repo)
    repo.upsert_article(
        article.model_copy(
            update={"url": "https://collected.example/origin-test", "is_sample": False}
        )
    )
    mixed = bundle_from_repository(repo)
    assert mixed.case.synthetic is False
    assert mixed.case.coverage["content_provenance"] == "mixed"
    selected = next(finding for finding in mixed.findings if finding.details["synthetic"] is True)
    service = make_service(settings, mixed)
    queued = service.submit(mixed.case.id, selected.snapshot_id)
    assert queued.payload.source_synthetic is True
    assert queued.payload.source_case_synthetic is False
    assert queued.payload.origin_records
    assert all(record.metadata["synthetic"] is True for record in queued.payload.origin_records)
    assert_origin(emit(queued), True, False)


def test_declared_real_finding_does_not_inherit_synthetic_case_flag(settings):
    source = source_bundle(True, False)
    queued = submit(make_service(settings, source), source)
    assert queued.payload.source_synthetic is False
    assert queued.payload.source_case_synthetic is True
    assert_origin(emit(queued), False, True)


@pytest.mark.parametrize("case_origin", [False, True])
@pytest.mark.parametrize("finding_origin", [MISSING, None, "true", "false", 1, 0])
def test_absent_or_non_boolean_finding_origin_stays_unknown(settings, case_origin, finding_origin):
    source = source_bundle(case_origin, finding_origin)
    queued = submit(make_service(settings, source), source)
    assert queued.payload.source_synthetic is None
    assert queued.payload.source_case_synthetic is case_origin
    assert_origin(emit(queued), None, case_origin)


def test_original_record_flags_and_missing_fields_are_not_rewritten(settings):
    values = [True, False, MISSING, None, "true", "false", 1, 0]
    source = source_bundle(True, False, record_origins=values)
    queued = submit(make_service(settings, source), source)
    result = emit(queued)
    origins = {record.metadata["original_record_id"]: record for record in result.records}
    assert len(origins) == len(source.records)
    for original in source.records:
        retained = origins[original.id]
        assert retained.text == original.text
        assert retained.content_hash == original.content_hash
        assert retained.metadata["claim_origin"] is True
        if "synthetic" not in original.metadata:
            assert "synthetic" not in retained.metadata
        else:
            expected, actual = original.metadata["synthetic"], retained.metadata["synthetic"]
            assert type(actual) is type(expected)
            assert actual == expected
    assert_origin(result, False, True)


@pytest.mark.parametrize("case_origin", [False, True])
@pytest.mark.parametrize("finding_origin", [True, False, MISSING, "true", 1])
def test_legacy_payload_uses_old_flag_only_for_case_origin(settings, case_origin, finding_origin):
    source = source_bundle(case_origin, finding_origin)
    queued = submit(make_service(settings, source), source)
    legacy = queued.payload.model_dump(mode="json")
    legacy.pop("source_case_synthetic")
    legacy["source_synthetic"] = case_origin
    restored = ClaimCheckPayload.model_validate_json(json.dumps(legacy))
    expected = finding_origin if type(finding_origin) is bool else None
    assert restored.claim_synthetic is expected
    assert restored.case_synthetic is case_origin
    serialized_again = ClaimCheckPayload.model_validate_json(restored.model_dump_json())
    assert serialized_again.claim_synthetic is expected
    assert serialized_again.case_synthetic is case_origin
    result = emit(queued.model_copy(update={"payload": serialized_again}))
    assert_origin(result, expected, case_origin)


@pytest.mark.parametrize("case_origin,finding_origin", [(False, True), (True, MISSING)])
def test_preexisting_queued_payload_remains_processable_with_new_optional_field(
    settings, case_origin, finding_origin
):
    source = source_bundle(case_origin, finding_origin)
    service = make_service(settings, source)
    queued = submit(service, source)
    legacy = queued.payload.model_dump(mode="json")
    legacy.pop("source_case_synthetic")
    legacy["source_synthetic"] = case_origin
    with service.storage.connection(write=True) as connection:
        connection.execute(
            "UPDATE claim_check_jobs SET payload_json=? WHERE id=?", (json.dumps(legacy), queued.id)
        )
    claimed = service.storage.claim()
    service.storage.validate_context(claimed)
    snapshot = EvidenceSnapshot(created_at=NOW)
    service.storage.freeze(claimed, snapshot)
    assessment = ProposedAssessment(
        status="insufficient_evidence",
        explanation="No external evidence requested.",
        provider="rules",
    )
    result_bundle = build_result_bundle(claimed, snapshot, assessment)
    result = ClaimCheckResult(
        result_case_id=result_bundle.case.id,
        result_finding_id=result_bundle.findings[0].snapshot_id,
        assessment=assessment,
        evidence_fingerprint=snapshot.fingerprint(),
        coverage="empty",
        sources_count=0,
    )
    service.storage.complete(claimed, result, result_bundle)
    assert service.get(queued.id).status == "completed"
    expected = finding_origin if type(finding_origin) is bool else None
    assert_origin(service.repository.get_bundle(result_bundle.case.id), expected, case_origin)


@pytest.mark.parametrize(
    "case_origin,finding_origin", [(False, True), (True, False), (True, MISSING)]
)
def test_rechecking_a_saved_result_preserves_known_or_unknown_claim_origin(
    settings, case_origin, finding_origin
):
    source = source_bundle(case_origin, finding_origin)
    service = make_service(settings, source)
    first = emit(submit(service, source))
    service.repository.import_bundle(first)
    second_job = submit(service, first)
    expected = finding_origin if type(finding_origin) is bool else None
    assert second_job.payload.source_synthetic is expected
    assert_origin(emit(second_job), expected, first.case.synthetic)


@pytest.mark.parametrize(
    "case_field_present", [False, True], ids=["legacy-absent", "explicit-null"]
)
def test_absence_and_explicit_unknown_case_origin_survive_retry_without_weakening_fencing(
    settings, case_field_present
):
    source = source_bundle(True, False)
    service = make_service(settings, source)
    queued = submit(service, source)
    envelope = queued.payload.model_dump(mode="json")
    envelope["source_synthetic"] = True
    if case_field_present:
        envelope["source_case_synthetic"] = None
    else:
        envelope.pop("source_case_synthetic")
    expected_case = None if case_field_present else True
    with service.storage.connection(write=True) as connection:
        connection.execute(
            "UPDATE claim_check_jobs SET payload_json=? WHERE id=?",
            (json.dumps(envelope), queued.id),
        )
    claimed = service.storage.claim()
    assert claimed.payload.case_synthetic is expected_case
    assert claimed.payload.claim_synthetic is False
    serialized = ClaimCheckPayload.model_validate_json(claimed.payload.model_dump_json())
    assert serialized.case_synthetic is expected_case
    service.storage.validate_context(claimed)
    frozen = EvidenceSnapshot(created_at=NOW)
    service.storage.freeze(claimed, frozen)
    service.storage.fail(claimed, "Simulated interruption after evidence freeze.")
    retried = service.retry(claimed.id)
    assert retried.id != claimed.id
    assert retried.payload.case_synthetic is expected_case
    assert retried.payload.claim_synthetic is False
    assert retried.snapshot.fingerprint() == frozen.fingerprint()
    assert_origin(emit(retried), False, expected_case)
    active_retry = service.storage.claim()
    service.storage.validate_context(active_retry)
    changed = active_retry.payload.model_dump(mode="json")
    changed["source_case_synthetic"] = False
    with service.storage.connection(write=True) as connection:
        connection.execute(
            "UPDATE claim_check_jobs SET payload_json=? WHERE id=?",
            (json.dumps(changed), active_retry.id),
        )
    with pytest.raises(StaleClaimCheck, match="attempt"):
        service.storage.validate_context(active_retry)


def legacy_completed_case(
    case_origin, *, pinned_origin=MISSING, top_origin=MISSING, include_snapshot=True
):
    bundle = source_bundle(case_origin, top_origin)
    finding = bundle.findings[0]
    details = dict(finding.details)
    details["domain"] = "claim_check"
    check = {"source_synthetic": case_origin}
    if include_snapshot:
        pinned = finding.model_dump(mode="json")
        pinned["details"] = {} if pinned_origin is MISSING else {"synthetic": pinned_origin}
        check["source_finding_snapshot"] = pinned
    details["claim_check"] = check
    finding.details = details
    return bundle


@pytest.mark.parametrize("pinned_origin", [True, False])
def test_legacy_completed_result_recheck_uses_exact_pinned_finding_origin(settings, pinned_origin):
    legacy = legacy_completed_case(not pinned_origin, pinned_origin=pinned_origin)
    assert "synthetic" not in legacy.findings[0].details
    service = make_service(settings, legacy)
    job = submit(service, legacy)
    assert job.payload.source_synthetic is pinned_origin
    assert job.payload.source_case_synthetic is (not pinned_origin)
    result = emit(job)
    assert_origin(result, pinned_origin, not pinned_origin)
    service.repository.import_bundle(result)
    repeated = submit(service, result)
    assert repeated.payload.source_synthetic is pinned_origin
    assert_origin(emit(repeated), pinned_origin, result.case.synthetic)


@pytest.mark.parametrize("pinned_origin", [True, False])
@pytest.mark.parametrize("top_origin", [None, "false", 0])
def test_explicit_unknown_or_non_boolean_top_origin_blocks_legacy_fallback(
    settings, pinned_origin, top_origin
):
    legacy = legacy_completed_case(
        not pinned_origin, pinned_origin=pinned_origin, top_origin=top_origin
    )
    job = submit(make_service(settings, legacy), legacy)
    assert job.payload.source_synthetic is None
    assert_origin(emit(job), None, not pinned_origin)


@pytest.mark.parametrize("case_origin", [False, True])
@pytest.mark.parametrize("include_snapshot", [False, True])
def test_legacy_case_flag_without_pinned_claim_flag_stays_unknown(
    settings, case_origin, include_snapshot
):
    legacy = legacy_completed_case(case_origin, include_snapshot=include_snapshot)
    job = submit(make_service(settings, legacy), legacy)
    assert job.payload.source_synthetic is None
    assert_origin(emit(job), None, case_origin)


def test_pinned_origin_fallback_is_limited_to_claim_check_results(settings):
    source = legacy_completed_case(False, pinned_origin=True)
    source.findings[0].details["domain"] = "another_producer"
    job = submit(make_service(settings, source), source)
    assert job.payload.source_synthetic is None
    assert_origin(emit(job), None, False)


@pytest.mark.parametrize("top_origin", [True, False])
def test_explicit_boolean_top_origin_takes_precedence_over_legacy_snapshot(settings, top_origin):
    legacy = legacy_completed_case(
        not top_origin, pinned_origin=not top_origin, top_origin=top_origin
    )
    job = submit(make_service(settings, legacy), legacy)
    assert job.payload.source_synthetic is top_origin
    assert_origin(emit(job), top_origin, not top_origin)
