"""Import boundaries are exercised through the same service used by the upload UI."""

import base64
import json
from copy import deepcopy

import pytest

from evidence_explorer.review.demo import load_demo_bundle
from evidence_explorer.review.service import ReviewService
from evidence_explorer.review.storage import ReviewRepository


@pytest.fixture
def import_desk(tmp_path):
    repository = ReviewRepository(tmp_path / "investigations.sqlite3")
    repository.initialize()
    return repository, ReviewService(repository)


def investigation():
    """Test-only content uses the non-demo import path, without real personal data."""
    data = load_demo_bundle().model_dump(mode="json")
    data["case"].update(
        id="import-test:case",
        title="Imported evidence · München",
        synthetic=False,
        limitations=["Test records only; original collection coverage is unknown."],
    )
    return data


def upload(data):
    raw = json.dumps(data, ensure_ascii=False).encode()
    return "data:application/json;base64," + base64.b64encode(raw).decode()


@pytest.mark.parametrize("form", ["text", "bytes", "upload"])
def test_preview_is_read_only_and_import_survives_restart(import_desk, form):
    repository, service = import_desk
    data = investigation()
    content = json.dumps(data, ensure_ascii=False)
    if form == "bytes":
        content = content.encode("utf-8")
    elif form == "upload":
        content = upload(data)
    summary = service.preview_import(content, "investigation.json")
    assert summary["title"] == data["case"]["title"]
    assert summary["synthetic"] is False
    assert summary["counts"]["records"] == 10
    assert summary["counts"]["findings"] == 3
    assert summary["limitations"] == data["case"]["limitations"]
    assert summary["status"] == "new"
    assert service.list_cases() == []
    case_id = service.commit_import(
        content, "investigation.json", expected_fingerprint=summary["fingerprint"]
    )
    reopened = ReviewService(ReviewRepository(repository.db_path))
    assert case_id == data["case"]["id"]
    assert reopened.get_case(case_id)["records"] == data["records"]
    assert reopened.preview_import(content)["status"] == "existing"
    assert reopened.commit_import(content, expected_fingerprint=summary["fingerprint"]) == case_id
    assert len(reopened.list_cases()) == 1


@pytest.mark.parametrize(
    "content",
    ["{", "[]", "null", b"\xff\xfe", "data:application/json;base64,!invalid!"],
)
def test_invalid_document_never_creates_a_case(import_desk, content):
    _, service = import_desk
    with pytest.raises(ValueError):
        service.preview_import(content, "untrusted.json")
    assert service.list_cases() == []


def test_document_size_limit_applies_to_decoded_upload_and_pasted_utf8(import_desk):
    from evidence_explorer.review.importing import MAX_IMPORT_BYTES

    _, service = import_desk
    oversized = "é" * (MAX_IMPORT_BYTES // 2 + 1)
    for content in (
        oversized,
        "data:application/json;base64," + base64.b64encode(oversized.encode()).decode(),
    ):
        with pytest.raises(ValueError, match=r"(?i)(large|limit|size|MB)"):
            service.preview_import(content)
    assert service.list_cases() == []


@pytest.mark.parametrize("defect", ["duplicate", "unknown_record", "quotation", "hash"])
def test_invalid_evidence_is_rejected_without_echoing_source_text(import_desk, defect):
    _, service = import_desk
    data = investigation()
    secret_excerpt = "private source excerpt must not appear in a validation error"
    if defect == "duplicate":
        data["records"].append(deepcopy(data["records"][0]))
    elif defect == "unknown_record":
        data["fragments"][0]["record_id"] = "missing-record"
    elif defect == "quotation":
        data["fragments"][0]["quote"] = secret_excerpt
    else:
        data["records"][0]["text"] = secret_excerpt
    with pytest.raises(ValueError) as error:
        service.preview_import(json.dumps(data))
    assert secret_excerpt not in str(error.value)
    assert service.list_cases() == []


def test_changed_document_cannot_be_committed_under_an_earlier_preview(import_desk):
    _, service = import_desk
    data = investigation()
    preview = service.preview_import(json.dumps(data))
    data["case"]["title"] = "Changed after preview"
    with pytest.raises(ValueError, match=r"(?i)(changed|preview|fingerprint)"):
        service.commit_import(json.dumps(data), expected_fingerprint=preview["fingerprint"])
    assert service.list_cases() == []


def test_conflicting_import_cannot_partially_add_a_record(import_desk):
    _, service = import_desk
    data = investigation()
    preview = service.preview_import(json.dumps(data))
    case_id = service.commit_import(json.dumps(data), expected_fingerprint=preview["fingerprint"])
    baseline = service.get_case(case_id)
    conflicting = deepcopy(data)
    conflicting["records"].append({**conflicting["records"][0], "id": "new-record"})
    conflicting["findings"][0]["statement"] = "Changed immutable finding"
    # A conflicting candidate may fail in preview or at commit; neither may write it.
    with pytest.raises(ValueError):
        candidate = service.preview_import(json.dumps(conflicting))
        service.commit_import(
            json.dumps(conflicting), expected_fingerprint=candidate["fingerprint"]
        )
    assert service.get_case(case_id) == baseline


def test_additive_import_preserves_saved_review(import_desk):
    _, service = import_desk
    data = investigation()
    raw = json.dumps(data)
    case_id = service.commit_import(
        raw, expected_fingerprint=service.preview_import(raw)["fingerprint"]
    )
    scenario = service.start_scenario(case_id, "harbor:c3-v1")
    service.review(case_id, "harbor:c3-v1", scenario["id"], "withhold", "Keep this review history")
    data["records"].append({**data["records"][0], "id": "new-record"})
    candidate = service.preview_import(json.dumps(data))
    assert candidate["status"] == "update"
    service.commit_import(json.dumps(data), expected_fingerprint=candidate["fingerprint"])
    assert len(service.get_case(case_id)["records"]) == 11
    assert any(
        event["event_type"] == "finding_reviewed"
        and event["payload"]["reason"] == "Keep this review history"
        for event in service.repository.list_events(case_id)
    )


@pytest.mark.parametrize(
    "raw", ['{"case": {}, "case": {}}', '{"value": NaN}', '{"value": Infinity}']
)
def test_ambiguous_or_nonstandard_json_is_rejected(import_desk, raw):
    _, service = import_desk
    with pytest.raises(ValueError):
        service.preview_import(raw)
    assert service.list_cases() == []


def test_empty_investigation_is_importable_but_preview_explains_missing_evidence(import_desk):
    _, service = import_desk
    data = investigation()
    for field in ("records", "fragments", "runs", "findings", "links", "reviews", "scenarios"):
        data[field] = []
    raw = json.dumps(data)
    summary = service.preview_import(raw)
    assert summary["counts"]["findings"] == 0
    assert summary["counts"]["records"] == 0
    assert any("No findings" in warning for warning in summary["warnings"])
    case_id = service.commit_import(raw, expected_fingerprint=summary["fingerprint"])
    assert service.get_case(case_id)["findings"] == []


def test_text_upload_cannot_smuggle_unverified_media_references(import_desk):
    _, service = import_desk
    data = investigation()
    data["records"][0]["artifact_ref"] = "private/source.jpg"
    with pytest.raises(ValueError, match=r"(?i)(media|artifact|saved text)"):
        service.preview_import(json.dumps(data))
    assert service.list_cases() == []
