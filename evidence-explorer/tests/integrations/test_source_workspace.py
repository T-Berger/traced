"""Mixed-source snapshots preserve review history and preview/commit boundaries."""

import json
import sqlite3

import pytest

from evidence_explorer.config import Settings
from evidence_explorer.review.integrations.workspace import WorkspaceRepository, WorkspaceService


def workspace(path):
    settings = Settings(data_dir=path)
    repository = WorkspaceRepository(settings.db_path)
    repository.initialize()
    return WorkspaceService(repository, settings, path / "artifacts")


@pytest.fixture
def source_content():
    return json.dumps(
        {
            "source_schema": "1.0",
            "case": {
                "title": "Mixed-source evidence",
                "question": "Where did the supplied statement appear?",
                "synthetic": True,
            },
            "items": [
                {
                    "platform": platform,
                    "channel_id": "source-1",
                    "channel_name": f"Fictional {platform} source",
                    "native_id": "post-1",
                    "text": "A fictional bridge closure was announced for the exercise.",
                    "source_url": f"https://{platform}.example/post-1",
                    "published_at": published,
                    "acquired_at": "2026-11-06T12:00:00Z",
                }
                for platform, published in (
                    ("telegram", "2026-11-06T10:00:00Z"),
                    ("news", None),
                )
            ],
        }
    )


def test_source_preview_is_read_only_and_commit_requires_exact_preview(tmp_path, source_content):
    service = workspace(tmp_path / "desk")
    summary = service.preview_source_import(source_content, "sources.json")
    assert summary["counts"]["records"] == 2
    assert summary["synthetic"] is True
    assert service.list_cases() == []
    changed = json.loads(source_content)
    changed["items"][0]["text"] = "Changed saved source text."
    with pytest.raises(ValueError, match="changed after validation"):
        service.commit_source_import(
            json.dumps(changed), expected_fingerprint=summary["fingerprint"]
        )
    assert service.list_cases() == []
    with sqlite3.connect(service.repository.db_path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM review_scenarios").fetchone()[0] == 0
        assert connection.execute("SELECT COUNT(*) FROM review_jobs").fetchone()[0] == 0


def test_source_reimport_preserves_review_and_package_roundtrip(tmp_path, source_content):
    service = workspace(tmp_path / "desk")
    summary = service.preview_source_import(source_content)
    case_id = service.commit_source_import(
        source_content, expected_fingerprint=summary["fingerprint"]
    )
    bundle = service.load_case(case_id)
    assert len({record.id for record in bundle.records}) == 2
    assert sum(record.published_at is None for record in bundle.records) == 1
    scenario = service.baseline_scenario(case_id, bundle.findings[0].snapshot_id)
    decision = service.review(
        case_id,
        bundle.findings[0].snapshot_id,
        scenario["id"],
        "qualify",
        "These are saved appearances; the original source is unverified.",
    )
    assert service.preview_source_import(source_content)["status"] == "existing"
    assert (
        service.commit_source_import(source_content, expected_fingerprint=summary["fingerprint"])
        == case_id
    )
    assert len(service.list_cases()) == 1
    assert service.view(case_id)["bundle"]["records"]
    exported = service.export_bundle(case_id)
    assert any(review.id == decision["id"] for review in exported.reviews)
    restored = workspace(tmp_path / "restored")
    assert restored.import_package(service.export_package(case_id)) == case_id
    assert restored.load_case(case_id).model_dump() == exported.model_dump()


def test_changed_source_snapshot_retains_original_case(tmp_path, source_content):
    service = workspace(tmp_path / "desk")
    first = service.preview_source_import(source_content)
    original_id = service.commit_source_import(
        source_content, expected_fingerprint=first["fingerprint"]
    )
    original = service.load_case(original_id).model_dump()
    changed = json.loads(source_content)
    changed["items"][0]["text"] = "A revised statement with different saved text."
    revised = json.dumps(changed)
    second = service.preview_source_import(revised)
    revised_id = service.commit_source_import(revised, expected_fingerprint=second["fingerprint"])
    assert original_id != revised_id
    assert len(service.list_cases()) == 2
    assert service.load_case(original_id).model_dump() == original
