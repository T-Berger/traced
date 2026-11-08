"""Read-only import of an explicitly selected collector snapshot."""

import hashlib
import json
import sqlite3
import subprocess
import sys

import pytest

from evidence_explorer.compliance_review.collector_snapshot import (
    build_collector_snapshot_bundle,
    import_new_collector_case,
)
from evidence_explorer.config import Settings
from evidence_explorer.review.integrations.workspace import WorkspaceRepository, WorkspaceService
from evidence_explorer.social_dashboard.projection import project_dashboard


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


@pytest.fixture
def collector(tmp_path):
    path = tmp_path / "fixture.sqlite"
    with sqlite3.connect(path) as conn:
        conn.executescript(
            """
            CREATE TABLE cases (id TEXT, slug TEXT, subject_name TEXT,
                window_start TEXT, window_end TEXT);
            CREATE TABLE authors (id TEXT, platform TEXT, platform_user_id TEXT,
                handle TEXT, raw TEXT);
            CREATE TABLE collection_runs (id TEXT, case_id TEXT, status TEXT,
                items_returned INTEGER);
            CREATE TABLE content_items (id TEXT, case_id TEXT, platform TEXT, platform_item_id TEXT,
                author_id TEXT, posted_at TEXT, collected_at TEXT, url TEXT, text TEXT,
                content_sha256 TEXT, labels TEXT, collection_run_id TEXT, raw TEXT);
            CREATE TABLE export_info (key TEXT, value TEXT);
            CREATE TABLE media_assets (id TEXT, content_item_id TEXT, source_url TEXT, raw TEXT);
            INSERT INTO cases VALUES
                ('case-src','fixture','Fictional subject','2025-01-01','2025-02-01');
            INSERT INTO authors VALUES
                ('author-1','twitter','native-author','fixture','{"supplied":true}');
            INSERT INTO collection_runs VALUES ('run-1','case-src','succeeded',2);
            INSERT INTO export_info VALUES ('exported_at','2026-01-01T00:00:00Z');
            INSERT INTO media_assets VALUES ('media-1','item-1','https://example.com/photo','{"supplied":true}');
            """
        )
        conn.execute(
            "INSERT INTO content_items VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                "item-1",
                "case-src",
                "twitter",
                "native-1",
                "author-1",
                "2025-01-05T10:00:00Z",
                "2025-01-06T10:00:00Z",
                "https://example.com/1",
                "A fictional saved post.",
                None,
                "{}",
                "run-1",
                '{"original":1}',
            ),
        )
        conn.execute(
            "INSERT INTO content_items VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                "item-2",
                "case-src",
                "twitter",
                "native-2",
                None,
                None,
                None,
                "https://example.com/2",
                "A second fictional post.",
                None,
                "{}",
                "run-1",
                '{"original":2}',
            ),
        )
    return path


def build(path, **overrides):
    options = {
        "expected_sha256": digest(path),
        "source_case_slug": "fixture",
        "new_case_id": "review:fixture",
        "provenance": {"location": "user-supplied fixture"},
    }
    options.update(overrides)
    return build_collector_snapshot_bundle(path, **options)


def test_snapshot_is_pinned_and_preserves_source_fields_without_claiming_review(collector):
    before = digest(collector)
    bundle = build(collector)
    assert len(bundle.records) == 2 and len(bundle.runs[0].input_manifest) == 2
    first, second = bundle.records
    assert first.metadata["native_id"] == "native-1"
    assert first.metadata["author_id"] == "native-author"
    assert bundle.case.coverage["normalized_platform_sources"] == {"x": ["twitter"]}
    assert first.metadata["platform"] == "x"
    assert "NULL" in bundle.case.coverage["source_text_hashes"]
    assert "empty JSON" in bundle.case.coverage["source_labels"]
    assert first.metadata["source_media"][0]["source_url"] == "https://example.com/photo"
    assert "raw" not in first.metadata["source_media"][0]
    assert "raw" not in first.metadata["source_row"]
    assert (
        first.metadata["source_raw_sha256_derived"] == hashlib.sha256(b'{"original":1}').hexdigest()
    )
    assert first.metadata["source_item_id"] == "item-1"
    assert "content_items.id" in bundle.case.coverage["source_row_locator"]
    assert second.published_at is None and second.acquired_at is None
    assert second.metadata["source_author"] is None
    assert bundle.case.coverage["status"] == "unknown"
    assert bundle.case.coverage["collection_runs"][0]["status"] == "succeeded"
    assert bundle.runs[0].input_fingerprint == before
    assert not bundle.runs[0].full_inputs_available
    assert not bundle.reviews and not bundle.scenarios
    assert bundle.findings[0].kind == "observation"
    dashboard = {row.id: row for row in project_dashboard(bundle).rows}
    assert dashboard[first.id].platform == "x"
    assert dashboard[first.id].author_key is not None
    assert dashboard[second.id].author_key is None
    assert digest(collector) == before


def test_wrong_hash_and_missing_case_fail_without_mutation(collector):
    before = digest(collector)
    with pytest.raises(ValueError, match="does not match"):
        build(collector, expected_sha256="0" * 64)
    with pytest.raises(ValueError, match="slug"):
        build(collector, source_case_slug="other")
    assert digest(collector) == before


@pytest.mark.parametrize("mutation", ["different_platform", "duplicate_native_id"])
def test_ambiguous_collector_author_stays_unknown_in_dashboard(collector, mutation):
    with sqlite3.connect(collector) as connection:
        if mutation == "different_platform":
            connection.execute("UPDATE authors SET platform='reddit' WHERE id='author-1'")
        else:
            connection.execute(
                "INSERT INTO authors VALUES (?,?,?,?,?)",
                ("author-2", "twitter", "native-author", "other", "{}"),
            )
    bundle = build(collector)
    first = bundle.records[0]
    assert first.metadata["source_author"]["platform_user_id"] == "native-author"
    assert "author_id" not in first.metadata
    dashboard = {row.id: row for row in project_dashboard(bundle).rows}
    assert dashboard[first.id].platform == "x"
    assert dashboard[first.id].author_key is None


def test_import_requires_a_new_case_and_preserves_previous_case(collector, tmp_path):
    bundle = build(collector)
    settings = Settings(data_dir=tmp_path / "workspace")
    repository = WorkspaceRepository(settings.db_path)
    repository.initialize()
    service = WorkspaceService(repository, settings, settings.data_dir / "artifacts")
    assert import_new_collector_case(service, bundle) == bundle.case.id
    before = repository.get_case_fingerprint(bundle.case.id)
    with pytest.raises(ValueError, match="already exists"):
        import_new_collector_case(service, bundle)
    assert repository.get_case_fingerprint(bundle.case.id) == before
    assert len(repository.get_bundle(bundle.case.id).records) == 2
    archive = service.export_package(bundle.case.id)
    recipient_settings = Settings(data_dir=tmp_path / "recipient")
    recipient_repository = WorkspaceRepository(recipient_settings.db_path)
    recipient_repository.initialize()
    recipient = WorkspaceService(
        recipient_repository, recipient_settings, recipient_settings.data_dir / "artifacts"
    )
    recipient.import_package(archive)
    reopened = recipient.export_bundle(bundle.case.id)
    assert [record.content_hash for record in reopened.records] == [
        record.content_hash for record in bundle.records
    ]
    assert not reopened.reviews


def test_cli_creates_only_new_workspace_and_retains_source_copy(collector, tmp_path):
    destination = tmp_path / "new-workspace"
    command = [
        sys.executable,
        "-m",
        "evidence_explorer.compliance_review.collector_snapshot",
        "--source",
        str(collector),
        "--sha256",
        digest(collector),
        "--source-case",
        "fixture",
        "--case-id",
        "review:fixture",
        "--data-dir",
        str(destination),
    ]
    completed = subprocess.run(command, capture_output=True, text=True, check=True)
    result = json.loads(completed.stdout)
    assert result["saved_records"] == 2
    assert result["coverage"] == "unknown"
    assert result["classification_or_human_review_created"] is False
    assert digest(destination / "source" / collector.name) == digest(collector)
    rejected = subprocess.run(command, capture_output=True, text=True, check=False)
    assert rejected.returncode != 0
    assert "already exists" in rejected.stderr
    assert digest(destination / "source" / collector.name) == digest(collector)
