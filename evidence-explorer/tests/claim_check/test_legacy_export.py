"""Both brief formats preserve per-record origin in mixed-provenance cases."""

import hashlib

import pytest

from evidence_explorer.config import Settings
from evidence_explorer.review.contracts import EvidenceBundle
from evidence_explorer.review.integrations.workspace import WorkspaceRepository, WorkspaceService


@pytest.mark.parametrize("claim_check", [True, False])
def test_briefs_and_package_reimport_preserve_record_provenance(tmp_path, claim_check):
    settings = Settings(data_dir=tmp_path)
    repository = WorkspaceRepository(settings.db_path)
    repository.initialize()
    service = WorkspaceService(repository, settings, tmp_path / "artifacts")
    records = []
    for identifier, provenance in [("original", True), ("collected", False), ("unknown", None)]:
        text = f"Offline fixture for {identifier} source."
        records.append(
            {
                "id": identifier,
                "kind": "article",
                "source_label": identifier,
                "text": text,
                "content_hash": hashlib.sha256(text.encode()).hexdigest(),
                "source_url": f"https://example.org/{identifier}",
                "metadata": {} if provenance is None else {"synthetic": provenance},
            }
        )
    bundle = EvidenceBundle.model_validate(
        {
            "case": {
                "id": "mixed-test",
                "title": "Mixed provenance test",
                "question": "Check the claim.",
                "synthetic": True,
            },
            "records": records,
            "fragments": [],
            "runs": [
                {
                    "id": "run",
                    "input_fingerprint": "fixture",
                    "input_manifest": [record["id"] for record in records],
                    "method": "offline_test",
                    "method_version": "1",
                    "created_at": "2026-11-06T12:00:00Z",
                }
            ],
            "findings": [
                {
                    "snapshot_id": "finding",
                    "lineage_id": "claim",
                    "version": 1,
                    "run_id": "run",
                    "kind": "inference",
                    "statement": "A synthetic claim for an offline export test.",
                    "details": {"domain": "claim_check"} if claim_check else {},
                }
            ],
            "links": [
                {
                    "id": f"link-{record['id']}",
                    "source": {"type": "record", "id": record["id"]},
                    "target": {"type": "finding", "id": "finding"},
                    "relation": "supports",
                    "basis": "Offline fixture link.",
                }
                for record in records
            ],
        }
    )
    service.import_bundle(bundle.model_dump(mode="json"))
    assert service.repository.get_bundle(bundle.case.id) == bundle
    scenario = service.start_scenario(bundle.case.id)
    saved = service.review(
        bundle.case.id,
        "finding",
        scenario["id"],
        "qualify",
        "Offline review: provenance is recorded separately for every source.",
    )

    def check_brief(exported):
        assert "fictional evidence and findings" not in exported
        assert "**Selected case: Synthetic example.**" in exported
        assert "This label applies to the selected case." in exported
        assert "Saved records retain their own provenance" in exported
        assert "collected sources are not labeled fictional" in exported
        assert "Test claim · collected sources retain their own provenance" not in exported
        if claim_check:
            assert "Claim origin not specified." in exported
            assert "Synthetic test claim" not in exported
        else:
            assert "Claim origin" not in exported
        assert "Record provenance: Synthetic record." in exported
        assert "Record provenance: Collected source; factual accuracy requires review." in exported
        assert "Record provenance: Record origin not specified." in exported
        assert saved["reason"] in exported

    check_brief(service.export_brief(bundle.case.id))
    check_brief(service.export_brief(bundle.case.id, scenario_id=scenario["id"]))
    before = {
        record.id: record.metadata for record in service.export_bundle(bundle.case.id).records
    }
    restored_settings = Settings(data_dir=tmp_path / "reopened")
    restored_repository = WorkspaceRepository(restored_settings.db_path)
    restored_repository.initialize()
    restored = WorkspaceService(
        restored_repository, restored_settings, restored_settings.data_dir / "artifacts"
    )
    assert restored.import_package(service.export_package(bundle.case.id)) == bundle.case.id
    reopened = restored.repository.get_bundle(bundle.case.id)
    assert reopened.case.synthetic is True
    assert {record.id: record.metadata for record in reopened.records} == before
    assert before == {
        "original": {"synthetic": True},
        "collected": {"synthetic": False},
        "unknown": {},
    }
    assert any(review.id == saved["id"] for review in reopened.reviews)
    check_brief(restored.export_brief(bundle.case.id))
    imported_scenario_id = next(
        review.scenario_id for review in reopened.reviews if review.id == saved["id"]
    )
    check_brief(restored.export_brief(bundle.case.id, scenario_id=imported_scenario_id))
