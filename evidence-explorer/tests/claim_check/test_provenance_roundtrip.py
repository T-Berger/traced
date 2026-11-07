"""Frozen collected sources retain distinct origins through current handoff and reopening."""

import hashlib
import json
import sqlite3
import zipfile
from pathlib import Path

import pytest

from evidence_explorer.config import Settings
from evidence_explorer.review.contracts import EvidenceBundle
from evidence_explorer.review.controller import select_assessment, session_for_case
from evidence_explorer.review.integrations.handoff_service import (
    import_receipt_for,
    load_handoff_context,
    prepare_dossier,
    prepare_export,
)
from evidence_explorer.review.integrations.handoff_ui import render_import_receipt
from evidence_explorer.review.integrations.workspace import WorkspaceRepository, WorkspaceService
from evidence_explorer.review.presentation import source_inspector

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures/case_packages"
LIVE_JOB = FIXTURES / "live-job.json"
LIVE_JOB_SHA256 = "306cd0e8cef63ae74f4707aed2145fe66e3254127ba87b42731ff87278dac39e"
CASES = [
    (
        "mixed-reviewed-reference.zip",
        "5305dc295f7087e6f5d95b032582717499da2b8ebc9d3a62aac62a2ca40e2c8e",
        3,
        1,
        True,
    ),
    (
        "live-reviewed-case.zip",
        "caa5908785d36a7022a71ca12ee3947ca40de89c050704eab70384c9c1200bb7",
        4,
        2,
        False,
    ),
]


def workspace(path):
    settings = Settings(data_dir=path)
    repository = WorkspaceRepository(settings.db_path)
    repository.initialize()
    return WorkspaceService(repository, settings, path / "artifacts")


def text(node):
    if isinstance(node, str):
        return node
    if isinstance(node, (list, tuple)):
        return " ".join(text(child) for child in node)
    return text(getattr(node, "children", ""))


def table_hashes(service):
    result = {}
    with sqlite3.connect(f"file:{service.settings.db_path}?mode=ro", uri=True) as connection:
        for (name,) in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
        ).fetchall():
            cursor = connection.execute('SELECT * FROM "' + name.replace('"', '""') + '"')
            rows = sorted(
                json.dumps(list(row), default=lambda value: {"bytes": value.hex()})
                for row in cursor.fetchall()
            )
            payload = json.dumps(
                {"columns": [column[0] for column in cursor.description], "rows": rows}
            ).encode()
            result[name] = {"rows": len(rows), "sha256": hashlib.sha256(payload).hexdigest()}
    return result


def saved_review_state(service, bundle):
    state = session_for_case(service, bundle.case.id)
    select_assessment(service, state, bundle.reviews[-1].scenario_id)
    return state


def assert_live_nasa_source_unchanged(bundle):
    raw = LIVE_JOB.read_bytes()
    assert hashlib.sha256(raw).hexdigest() == LIVE_JOB_SHA256
    job = json.loads(raw)
    source = next(row for row in job["snapshot"]["records"] if row["publisher"] == "NASA Science")
    record = next(row for row in bundle.records if row.source_url == source["url"])
    assert record.text == source["text"]
    assert (
        record.content_hash
        == source["content_hash"]
        == hashlib.sha256(record.text.encode()).hexdigest()
    )
    assert record.published_at == source["published_at"]
    assert record.acquired_at == source["retrieved_at"]
    assert record.metadata["synthetic"] is False
    assert record.metadata["collection_record_id"] == source["id"]
    # Collector provenance stays exact. The claim-check adapter separately
    # records independence as not established; this is not a new retrieval.
    for key, value in source["metadata"].items():
        if key != "independence":
            assert record.metadata[key] == value
    assert record.metadata["independence"] == (
        "not_established"
        if bundle.case.id.startswith("claim-check:")
        else source["metadata"]["independence"]
    )


def origin_labels(bundle):
    return {
        record.id: "Synthetic record"
        if record.metadata.get("synthetic") is True
        else "Collected source; factual accuracy requires review"
        if record.metadata.get("synthetic") is False
        else "Record origin not specified"
        for record in bundle.records
    }


def inspect_handoff(service, bundle, state):
    view, handoff, _ = load_handoff_context(service, state)
    assert handoff["dirty"] is False
    assert handoff["brief"]["included"], "The exact imported qualification must still apply"
    citations = [row for item in handoff["brief"]["included"] for row in item["evidence"]]
    origins = {record.id: record.metadata.get("synthetic") for record in bundle.records}
    assert citations
    for citation in citations:
        assert citation["record_synthetic"] is origins[citation["record_id"]]
    labels = origin_labels(bundle)
    for record in bundle.records:
        inspector = text(source_inspector(view["bundle"], record.id, state["finding_id"], []))
        assert f"Record provenance: {labels[record.id]}." in inspector
        if "synthetic" not in record.metadata:
            assert "Record provenance: Synthetic record." not in inspector
            assert (
                "Record provenance: Collected source; factual accuracy requires review."
                not in inspector
            )

    preview = handoff["download_context"]
    brief = prepare_export(service, "brief", state, expected_context=preview).content
    dossier = prepare_dossier(service, state, expected_context=preview).content
    assert brief in dossier
    assert "fictional evidence and findings" not in brief
    for record_id in {row["record_id"] for row in citations}:
        assert f"Record provenance: {labels[record_id]}." in brief
    if bundle.case.synthetic:
        assert "Selected case: Synthetic example." in brief
        assert "This label applies to the selected case." in brief
        assert "Saved records retain their own provenance" in brief
    else:
        assert "Selected case: Synthetic example." not in brief
        assert "Test claim · collected sources retain their own provenance" not in brief

    receipt = import_receipt_for(service, bundle.case.id)
    assert receipt["case"]["synthetic"] is bundle.case.synthetic
    assert receipt["validation"]["status"] == "validated_saved_snapshot"
    assert receipt["validation"]["recipient_acceptance"] == "not_recorded"
    assert receipt["counts"]["records"] == len(bundle.records)
    assert receipt["counts"]["reviews"] == len(bundle.reviews)
    receipt_text = text(render_import_receipt(receipt))
    assert "Recipient acceptance is not recorded." in receipt_text
    assert "fictional evidence and findings" not in receipt_text
    assert receipt["planning"]["status"] == "not_recorded"
    return handoff, brief, dossier, receipt


@pytest.mark.parametrize(
    ("package_path", "package_hash", "record_count", "review_count", "synthetic_case"),
    CASES,
    ids=["mixed-synthetic-collected-unspecified", "saved-live-claim-check"],
)
def test_saved_source_origins_survive_current_handoff_and_native_roundtrip(
    tmp_path, package_path, package_hash, record_count, review_count, synthetic_case
):
    package = FIXTURES / package_path
    raw = package.read_bytes()
    assert hashlib.sha256(raw).hexdigest() == package_hash
    with zipfile.ZipFile(package) as archive:
        original = EvidenceBundle.model_validate_json(archive.read("bundle.json"))
    assert len(original.records) == record_count
    assert len(original.reviews) == review_count
    assert len(original.scenarios) == 1
    assert original.case.synthetic is synthetic_case
    assert_live_nasa_source_unchanged(original)
    if synthetic_case:
        assert {record.metadata.get("synthetic") for record in original.records} == {
            True,
            False,
            None,
        }
        unspecified = next(
            record for record in original.records if "synthetic" not in record.metadata
        )
        assert unspecified.metadata["fixture_kind"] == "intentional_missing_provenance_input"
    else:
        assert original.case.id.startswith("claim-check:")
        assert original.findings[0].details["claim_check"]["source_synthetic"] is False
        assert all(record.metadata["synthetic"] is False for record in original.records)

    sender = workspace(tmp_path / "sender")
    assert sender.import_package(raw) == original.case.id
    state = saved_review_state(sender, original)
    before = table_hashes(sender)
    handoff, brief, dossier, receipt = inspect_handoff(sender, original, state)
    preview = handoff["download_context"]
    exported = prepare_export(sender, "package", state, expected_context=preview)
    evidence_json = prepare_export(sender, "json", state, expected_context=preview)
    assert EvidenceBundle.model_validate_json(evidence_json.content) == original
    assert sender.export_bundle(original.case.id) == original
    assert table_hashes(sender) == before

    recipient = workspace(tmp_path / "recipient")
    assert recipient.import_package(exported.content) == original.case.id
    reopened = recipient.export_bundle(original.case.id)
    assert reopened == original
    assert_live_nasa_source_unchanged(reopened)
    assert {row.id: row.metadata for row in reopened.records} == {
        row.id: row.metadata for row in original.records
    }
    restored_state = saved_review_state(recipient, reopened)
    recipient_before = table_hashes(recipient)
    _, restored_brief, restored_dossier, restored_receipt = inspect_handoff(
        recipient, reopened, restored_state
    )
    assert restored_brief == brief
    assert restored_dossier == dossier
    assert restored_receipt == receipt
    assert recipient.repository.list_events(original.case.id) == []
    assert recipient.planning_briefs.list() == ()
    assert table_hashes(recipient) == recipient_before
    assert hashlib.sha256(package.read_bytes()).hexdigest() == package_hash
