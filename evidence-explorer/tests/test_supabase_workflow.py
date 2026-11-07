"""Database snapshots use the existing immutable collection and portable-case flow."""

import hashlib
import json

import pytest
from pydantic import ValidationError

from evidence_explorer.collection.models import CollectedBatch
from evidence_explorer.collection.runtime import CollectionRuntime
from evidence_explorer.config import Settings
from evidence_explorer.review.integrations.workspace import WorkspaceRepository, WorkspaceService
from evidence_explorer.source_contracts import SourceAcquisition, SourceItem


def saved_item(text="An exact fixture passage."):
    raw = json.dumps({"id": "row-1", "content": text, "platform": "unknown"})
    return SourceItem(
        platform="supabase",
        channel_id="fixture/postgres/public.content_items",
        channel_name="Supabase · public.content_items",
        native_id="row-1",
        text=text,
        acquired_at="2026-11-07T12:00:00Z",
        provenance={
            "method": "supabase_postgres",
            "content_scope": "database_row",
            "database_row": {
                "project_ref": "fixture",
                "database": "postgres",
                "schema_name": "public",
                "table_name": "content_items",
                "id_column": "id",
                "text_column": "content",
                "row_json": raw,
                "row_sha256": hashlib.sha256(raw.encode()).hexdigest(),
            },
        },
    )


def workspace(path):
    settings = Settings(data_dir=path)
    repository = WorkspaceRepository(settings.db_path)
    repository.initialize()
    return WorkspaceService(repository, settings, path / "artifacts")


def test_collection_preview_case_export_and_reopen_preserve_raw_row(tmp_path, monkeypatch):
    monkeypatch.setenv("SUPABASE_HOST", "db.fixture.supabase.co")
    monkeypatch.setenv("SUPABASE_PASSWORD", "fixture-secret-not-in-evidence")
    service = workspace(tmp_path / "source")
    source_id = service.collection.add(
        {
            "platform": "supabase",
            "label": "Content items",
            "locator": "content_items",
            "enabled": False,
        }
    )
    item = saved_item()
    runtime = CollectionRuntime(
        service.collection.repository,
        collector=lambda source: CollectedBatch(items=(item,), warnings=("Bounded sample.",)),
    )
    service.collection.enqueue(source_id)
    assert runtime.run_once()
    preview = service.collection.preview_case([source_id])
    bundle = service.collection.scoped_bundle(
        [source_id], expected_fingerprint=preview["fingerprint"]
    )
    assert bundle.records[0].metadata["provenance"]["database_row"] == (
        item.provenance.database_row.model_dump()
    )
    assert bundle.records[0].published_at is None
    assert "fixture-secret-not-in-evidence" not in bundle.model_dump_json()
    service.import_bundle(bundle.model_dump(mode="json"))
    recipient = workspace(tmp_path / "recipient")
    recipient.import_package(service.export_package(bundle.case.id))
    assert recipient.export_bundle(bundle.case.id).records == bundle.records

    original = service.export_bundle(bundle.case.id).model_dump()
    changed = saved_item("A later database revision.")
    runtime.collector = lambda source: CollectedBatch(items=(changed,))
    service.collection.enqueue(source_id)
    assert runtime.run_once()
    changed_preview = service.collection.preview_case([source_id])
    assert changed_preview["fingerprint"] != preview["fingerprint"]
    assert service.export_bundle(bundle.case.id).model_dump() == original


def test_legacy_acquisition_serialization_preserves_previous_evidence_identity():
    old = {
        "method": "rss_content",
        "content_scope": "feed_content",
        "retrieved_url": "https://publisher.example/feed",
        "feed_url": None,
        "published_at_raw": None,
        "author_label": None,
    }
    assert SourceAcquisition.model_validate(old).model_dump(mode="json") == old


@pytest.mark.parametrize("change", ["text", "native_id", "row_json", "row_sha256"])
def test_mismatched_database_snapshot_is_rejected(change):
    data = saved_item().model_dump(mode="json")
    if change in {"text", "native_id"}:
        data[change] = "modified"
    else:
        data["provenance"]["database_row"][change] = "0" * 64
    with pytest.raises(ValidationError):
        SourceItem.model_validate(data)
