"""Selected collection scope remains visible and cannot borrow unrelated evidence."""

import sqlite3
from contextlib import closing, contextmanager
from copy import deepcopy
from datetime import UTC, datetime, timedelta

import pytest

from evidence_explorer.collection.models import CollectedBatch, CollectionFailure, CollectionSource
from evidence_explorer.collection.scoping import preview_scope, scoped_bundle
from evidence_explorer.config import Settings
from evidence_explorer.review.integrations.packages import export_case_package, import_case_package
from evidence_explorer.review.integrations.sources import SourceItem, build_source_bundle
from evidence_explorer.review.integrations.workspace import WorkspaceRepository, WorkspaceService

CLOCK = datetime(2035, 1, 2, 10, tzinfo=UTC)


def item(key="a", *, platform="news", text=None, published=None):
    return SourceItem(
        platform=platform,
        channel_id=f"channel-{key}",
        channel_name=f"Source {key}",
        native_id=f"post-{key}",
        text=text or f"Exact saved source {key} text about the public bridge inspection.",
        source_url=f"https://publisher.example/{key}",
        published_at=published,
        acquired_at=CLOCK.isoformat(),
    ).model_dump(mode="json")


def source(key="a", *, count=1, unknown=1, status="succeeded"):
    return {
        "id": f"source-{key}",
        "platform": "news",
        "label": f"Source {key}",
        "locator": f"https://publisher.example/{key}/rss",
        "item_count": count,
        "included_count": count,
        "unknown_publication_times": unknown,
        "last_status": status,
        "last_error": "The public source could not be fetched." if status == "failed" else None,
        "last_collected_at": CLOCK.isoformat() if count else None,
        "item_limit": 5,
        "content_mode": "feed_content",
    }


def selection(*, with_failed=False):
    sources = [source()]
    if with_failed:
        sources.append(source("failed", count=0, unknown=0, status="failed"))
    return {
        "source_ids": [row["id"] for row in sources],
        "sources": sources,
        "items": [item()],
        "total_items": 1,
        "truncated": False,
    }


def observation(source_id, key, *, attribution="observed"):
    return {
        "source_id": source_id,
        "item_key": key,
        "payload_hash": "1" * 64 if attribution == "observed" else None,
        "semantic_hash": "2" * 64 if attribution == "observed" else None,
        "content_hash": "3" * 64 if attribution == "observed" else None,
        "first_observed_at": CLOCK.isoformat() if attribution == "observed" else None,
        "observed_at": CLOCK.isoformat() if attribution == "observed" else None,
        "attribution": attribution,
    }


def versioned_selection():
    omitted = {
        "item_key": "shared-key",
        "channel_id": "shared-channel",
        "native_id": "shared-native",
        "observed_at": CLOCK.isoformat(),
        "payload_hash": "4" * 64,
        "content_hash": "5" * 64,
        "included_source_id": "source-b",
        "included_observed_at": CLOCK.isoformat(),
        "included_payload_hash": "6" * 64,
        "included_content_hash": "5" * 64,
        "observation_time_tied": True,
        "included_in_case": False,
        "reason": "Different saved payload; equal observation times use the source-ID convention.",
    }
    return {
        "source_ids": ["source-a", "source-b"],
        "sources": [
            {
                **source(),
                "included_count": 0,
                "unknown_publication_times": 0,
                "omitted_version_count": 1,
                "version_omissions": [omitted],
            },
            {**source("b", count=2), "included_count": 1},
        ],
        "items": [item("b")],
        "total_items": 2,
        "truncated": True,
        "version_policy": "latest_selected_observation_then_source_id",
        "observations_fingerprint": "d" * 64,
        "observation_manifest": [
            observation("source-a", "shared-key"),
            observation("source-b", "shared-key"),
            observation("source-b", "included-key"),
        ],
    }


def workspace(path):
    settings = Settings(data_dir=path)
    repository = WorkspaceRepository(settings.db_path)
    repository.initialize()
    return WorkspaceService(repository, settings, path / "artifacts")


def save_source(service, key, *, saved_item=None, failed=False):
    repository = service.collection.repository
    config = CollectionSource(
        id=f"source-{key}",
        platform="news",
        label=f"Source {key}",
        locator=f"https://publisher.example/{key}/rss",
        enabled=False,
    )
    repository.add(config, now=CLOCK)
    if saved_item is not None or failed:
        repository.enqueue(config.id, now=CLOCK)
        job = repository.claim(now=CLOCK)
        if failed:
            repository.fail(job, "The public source could not be fetched.", now=CLOCK)
        else:
            repository.complete(
                job, CollectedBatch((SourceItem.model_validate(saved_item),)), now=CLOCK
            )
    return config.id


def test_preview_retains_failed_empty_sources_and_unknown_publication_times():
    snapshot = selection(with_failed=True)
    original = deepcopy(snapshot)
    preview = preview_scope(snapshot)
    assert preview["source_ids"] == ["source-a", "source-failed"]
    assert preview["record_count"] == 1 and preview["unknown_publication_times"] == 1
    assert preview["can_create"] is True
    failed = next(row for row in preview["sources"] if row["id"] == "source-failed")
    assert failed["item_count"] == failed["included_count"] == 0
    assert failed["last_status"] == "failed" and failed["last_error"]
    assert failed["last_collected_at"] is None
    assert any("Source failed" in notice for notice in preview["limitations"])
    assert snapshot == original


@pytest.mark.parametrize(
    ("source_ids", "message"),
    [([], "at least one"), (["source-a", " source-a "], "only once"), (["unknown"], "no longer")],
)
def test_invalid_or_unknown_selection_never_silently_falls_back_to_all(source_ids, message):
    snapshot = selection()
    snapshot["source_ids"] = source_ids
    with pytest.raises(CollectionFailure, match=message):
        preview_scope(snapshot)


def test_empty_selected_sources_can_be_previewed_without_creating_evidence():
    snapshot = {
        "source_ids": ["source-failed"],
        "sources": [source("failed", count=0, unknown=0, status="failed")],
        "items": [],
        "total_items": 0,
        "truncated": False,
    }
    preview = preview_scope(snapshot)
    assert preview["record_count"] == 0 and preview["can_create"] is False
    assert "No saved records" in preview["reason"]
    with pytest.raises(CollectionFailure, match="No saved records"):
        scoped_bundle(snapshot)


def test_scope_is_stable_when_reordered_or_only_scheduler_fields_change():
    original = selection(with_failed=True)
    reordered = deepcopy(original)
    reordered["sources"].reverse()
    reordered["source_ids"].reverse()
    for row in reordered["sources"]:
        row.update(next_due_at="2099-01-01T00:00:00Z", job_id="different-job", enabled=True)
    assert preview_scope(reordered)["fingerprint"] == preview_scope(original)["fingerprint"]
    assert scoped_bundle(reordered).model_dump() == scoped_bundle(original).model_dump()


def test_changed_selection_or_material_coverage_creates_new_assessment_ids():
    first = scoped_bundle(selection())
    with_empty_failed = scoped_bundle(selection(with_failed=True))
    refreshed = selection()
    refreshed["sources"][0]["last_collected_at"] = "2035-01-03T10:00:00Z"
    after_refresh = scoped_bundle(refreshed)
    assert len({case.case.id for case in (first, with_empty_failed, after_refresh)}) == 3
    assert first.records == with_empty_failed.records == after_refresh.records
    for changed in (with_empty_failed, after_refresh):
        assert {run.id for run in first.runs}.isdisjoint(run.id for run in changed.runs)
        assert {f.snapshot_id for f in first.findings}.isdisjoint(
            f.snapshot_id for f in changed.findings
        )


def test_all_analysis_references_are_scoped_while_exact_record_ids_are_preserved():
    text = "Exact saved text — with Unicode punctuation and no invented wording."
    first, second = item(text=text), item("b", platform="telegram", text=text)
    second["repost_of"] = {"platform": "news", "channel_id": "channel-a", "native_id": "post-a"}
    snapshot = {
        "source_ids": ["source-a", "source-b"],
        "sources": [source(), {**source("b"), "platform": "telegram"}],
        "items": [first, second],
        "total_items": 2,
        "truncated": False,
    }
    bundle = scoped_bundle(snapshot)
    base = build_source_bundle(
        {
            "source_schema": "1.0",
            "case": {"title": "Base", "question": "Base question"},
            "items": [first, second],
        }
    )
    assert bundle.records == base.records and bundle.fragments == base.fragments
    run_ids, finding_ids = {run.id for run in bundle.runs}, {f.snapshot_id for f in bundle.findings}
    assert run_ids.isdisjoint(run.id for run in base.runs)
    assert finding_ids.isdisjoint(f.snapshot_id for f in base.findings)
    assert {link.relation for link in bundle.links} >= {
        "same_content",
        "documented_repost",
        "supports",
    }
    for link in bundle.links:
        assert link.run_id in run_ids
        for ref in (link.source, link.target):
            if ref.type == "finding":
                assert ref.id in finding_ids
    assert all(finding.run_id in run_ids for finding in bundle.findings)
    assert bundle.runs[0].input_fingerprint != base.runs[0].input_fingerprint
    assert bundle.runs[0].configuration["selected_source_ids"] == ["source-a", "source-b"]
    assert bundle.scenarios == bundle.reviews == []


def test_shared_items_are_deduplicated_and_case_cap_is_explicit():
    snapshot = selection(with_failed=True)
    snapshot["items"] = [item(), item()]
    assert preview_scope(snapshot)["record_count"] == 1
    assert len(scoped_bundle(snapshot).records) == 1
    capped = selection()
    capped["total_items"] = 3
    capped["sources"][0]["item_count"] = 3
    capped["truncated"] = True
    preview = preview_scope(capped)
    assert preview["truncated"] is True and preview["record_count"] == 1
    assert any("1 of 3" in notice for notice in preview["limitations"])
    conflicting = selection()
    conflicting["items"].append({**item(), "text": "Changed text under the same native identity."})
    with pytest.raises(CollectionFailure, match="conflicting"):
        preview_scope(conflicting)


def test_scope_metadata_and_empty_failures_survive_case_package_roundtrip(tmp_path):
    bundle = scoped_bundle(selection(with_failed=True))
    package = export_case_package(bundle)
    reopened = import_case_package(package, tmp_path / "artifacts")
    assert reopened.model_dump() == bundle.model_dump()
    scope = reopened.case.coverage["collection_scope"]
    assert scope["sources"][1]["last_status"] == "failed"
    assert scope["sources"][1]["included_count"] == 0
    assert reopened.collection_scope == scope


def test_saved_content_scope_survives_cleared_errors_and_configured_mode_changes(tmp_path):
    snapshot = selection()
    snapshot["sources"][0].update(
        content_mode="article_links", content_scopes={"feed_summary": 1}, last_error=None
    )
    snapshot["items"][0]["provenance"] = {
        "method": "rss_content",
        "content_scope": "feed_summary",
    }
    video = {**source("video"), "platform": "youtube", "content_scopes": {"title_description": 1}}
    snapshot["sources"].append(video)
    snapshot["source_ids"].append(video["id"])
    snapshot["items"].append(
        {
            **item("video", platform="youtube"),
            "provenance": {"method": "youtube_feed", "content_scope": "title_description"},
        }
    )
    snapshot["total_items"] = 2
    bundle = scoped_bundle(snapshot)
    reopened = import_case_package(export_case_package(bundle), tmp_path / "content-scope")
    scope = reopened.case.coverage["collection_scope"]
    assert scope["sources"][0]["content_scopes"] == {"feed_summary": 1}
    assert scope["sources"][0]["last_error"] is None
    assert scope["sources"][1]["content_scopes"] == {"title_description": 1}
    assert any("feed summaries, not complete articles" in text for text in scope["limitations"])
    assert any("not transcripts" in text for text in scope["limitations"])
    assert reopened.collection_scope == scope
    assert reopened.runs[0].coverage["collection_scope"] == scope


@pytest.mark.parametrize(
    "counts", [{"feed_summary": -1}, {"feed_summary": 0}, {"post_text": True}, {"invented": 1}]
)
def test_invalid_content_counts_are_rejected_without_guessing_from_status(counts):
    snapshot = selection()
    snapshot["sources"][0].update(content_scopes=counts, last_error="An article request failed.")
    with pytest.raises(CollectionFailure, match="invalid records or coverage"):
        preview_scope(snapshot)


def test_missing_content_scope_remains_unknown_even_when_configuration_names_articles():
    snapshot = selection()
    snapshot["sources"][0].update(content_mode="article_links", last_error=None)
    preview = preview_scope(snapshot)
    assert preview["sources"][0]["content_scopes"] == {"unknown": 1}
    assert any("unknown content scope" in text for text in preview["limitations"])


def test_connection_readiness_is_independent_of_saved_history_and_drops_secret_extras(tmp_path):
    snapshot = selection()
    snapshot["sources"][0].update(
        ready=False,
        connection_status="API access must be configured in local settings.",
        access_token="not-for-the-case",
        authorization_headers={"Authorization": "not-for-the-case"},
    )
    preview = preview_scope(snapshot)
    assert preview["can_create"] is True
    row = preview["sources"][0]
    assert row["ready"] is False and row["last_status"] == "succeeded"
    assert row["connection_status"] == "API access must be configured in local settings."
    assert "not-for-the-case" not in repr(preview)
    without_extras = deepcopy(snapshot)
    without_extras["sources"][0].pop("access_token")
    without_extras["sources"][0].pop("authorization_headers")
    assert preview_scope(without_extras) == preview
    bundle = scoped_bundle(snapshot)
    reopened = import_case_package(export_case_package(bundle), tmp_path / "readiness")
    assert reopened.case.coverage["collection_scope"]["sources"][0] == row
    unknown = preview_scope(selection())["sources"][0]
    assert unknown["ready"] is None and unknown["connection_status"] is None
    changed = deepcopy(snapshot)
    changed["sources"][0].update(ready=True, connection_status="Public source access is available.")
    assert scoped_bundle(changed).case.id != bundle.case.id
    assert scoped_bundle(changed).records == bundle.records


def test_legacy_membership_remains_unknown_and_cannot_supply_an_invented_record(tmp_path):
    snapshot = selection()
    snapshot.update(
        source_ids=["source-a", "source-b"],
        sources=[
            {
                **source(),
                "included_count": 0,
                "unknown_publication_times": 0,
                "unattributed_count": 1,
            },
            source("b"),
        ],
        items=[item("b")],
        version_policy="latest_selected_observation_then_source_id",
        observations_fingerprint="a" * 64,
        observation_manifest=[
            observation("source-a", "legacy-key", attribution="unknown"),
            observation("source-b", "known-key"),
        ],
    )
    bundle = scoped_bundle(snapshot)
    assert [record.metadata["native_id"] for record in bundle.records] == ["post-b"]
    reopened = import_case_package(export_case_package(bundle), tmp_path / "legacy")
    scope = reopened.case.coverage["collection_scope"]
    assert scope["sources"][0]["unattributed_count"] == 1
    assert scope["sources"][0]["content_scopes"] == {}
    assert scope["observation_manifest"][0]["attribution"] == "unknown"
    assert scope["observation_manifest"][0]["payload_hash"] is None
    assert any("Source a" in text and "provenance" in text for text in scope["limitations"])


def test_version_omissions_retain_hash_meanings_ties_and_case_limit_in_export(tmp_path):
    snapshot = versioned_selection()
    preview = preview_scope(snapshot)
    assert preview["can_create"] is True
    bundle = scoped_bundle(snapshot)
    reopened = import_case_package(export_case_package(bundle), tmp_path / "versions")
    scope = reopened.case.coverage["collection_scope"]
    assert scope == preview
    omitted = scope["sources"][0]["version_omissions"][0]
    assert omitted["included_in_case"] is False and omitted["observation_time_tied"] is True
    assert omitted["payload_hash"] != omitted["included_payload_hash"]
    assert omitted["content_hash"] == omitted["included_content_hash"]
    assert {record.metadata["native_id"] for record in reopened.records} == {"post-b"}
    assert {(row["source_id"], row["item_key"]): row for row in scope["observation_manifest"]} == {
        (row["source_id"], row["item_key"]): row for row in snapshot["observation_manifest"]
    }
    assert any("not included or available" in text for text in scope["limitations"])
    assert any("metadata differences" in text for text in scope["limitations"])
    assert any(
        "newest platform edit" in text and "selection convention" in text
        for text in scope["limitations"]
    )
    assert reopened.runs[0].configuration["selected_observations_fingerprint"] == "d" * 64


@pytest.mark.parametrize("missing", ["digest", "manifest", "manifest_row", "omission_details"])
def test_incomplete_saved_observation_provenance_blocks_case_creation(missing):
    snapshot = versioned_selection()
    if missing == "digest":
        snapshot.pop("observations_fingerprint")
    elif missing == "manifest":
        snapshot.pop("observation_manifest")
    elif missing == "manifest_row":
        snapshot["observation_manifest"].pop()
    else:
        snapshot["sources"][0]["version_omissions"] = []
    preview = preview_scope(snapshot)
    assert preview["can_create"] is False
    assert "incomplete version or provenance" in preview["reason"]
    with pytest.raises(CollectionFailure, match="incomplete version or provenance"):
        scoped_bundle(snapshot)


@pytest.mark.parametrize(
    ("field", "invalid"),
    [
        ("source_id", "unselected"),
        ("payload_hash", "not-a-hash"),
        ("observed_at", None),
        ("first_observed_at", "2035-01-02T10:00:00"),
        ("first_observed_at", "2036-01-02T10:00:00+00:00"),
    ],
)
def test_invalid_known_observation_manifest_is_rejected(field, invalid):
    snapshot = versioned_selection()
    snapshot["observation_manifest"][0][field] = invalid
    with pytest.raises(CollectionFailure):
        preview_scope(snapshot)


def test_manifest_order_and_equivalent_timezones_are_canonical_but_manifest_changes_are_material():
    snapshot = versioned_selection()
    reordered = deepcopy(snapshot)
    reordered["observation_manifest"].reverse()
    for row in reordered["observation_manifest"]:
        row["first_observed_at"] = row["observed_at"] = "2035-01-02T11:00:00+01:00"
    for row in reordered["sources"][0]["version_omissions"]:
        row["observed_at"] = row["included_observed_at"] = "2035-01-02T11:00:00+01:00"
    assert preview_scope(reordered) == preview_scope(snapshot)
    changed = deepcopy(snapshot)
    changed["observation_manifest"][0]["content_hash"] = "9" * 64
    assert scoped_bundle(changed).case.id != scoped_bundle(snapshot).case.id
    assert scoped_bundle(changed).records == scoped_bundle(snapshot).records


def test_repository_selection_omits_unrelated_items_and_preserves_saved_reviews(tmp_path):
    service = workspace(tmp_path / "desk")
    a = save_source(service, "a", saved_item=item())
    failed = save_source(service, "failed", failed=True)
    save_source(service, "unrelated", saved_item=item("unrelated"))
    snapshot = service.collection.repository.scope_snapshot([failed, a])
    first = scoped_bundle(snapshot)
    assert [record.metadata["native_id"] for record in first.records] == ["post-a"]
    assert first.case.coverage["collection_scope"]["source_ids"] == [a, failed]
    service.import_bundle(first.model_dump(mode="json"))
    assessment = service.baseline_scenario(first.case.id, first.findings[0].snapshot_id)
    decision = service.review(
        first.case.id,
        first.findings[0].snapshot_id,
        assessment["id"],
        "qualify",
        "The selected failed source contributes no saved evidence.",
    )
    original = service.export_bundle(first.case.id).model_dump()
    broader = scoped_bundle(
        service.collection.repository.scope_snapshot([a, failed, "source-unrelated"])
    )
    service.import_bundle(broader.model_dump(mode="json"))
    assert first.case.id != broader.case.id
    assert service.export_bundle(first.case.id).model_dump() == original
    assert service.import_bundle(first.model_dump(mode="json")) == first.case.id
    assert any(
        review.id == decision["id"] for review in service.export_bundle(first.case.id).reviews
    )
    restored = workspace(tmp_path / "restored")
    restored.import_package(service.export_package(first.case.id))
    assert restored.load_case(first.case.id).model_dump() == original


@pytest.mark.parametrize("selected", [[], ["source-a", "source-a"], ["missing"], [None]])
def test_repository_rejects_invalid_selection_without_writing_state(tmp_path, selected):
    service = workspace(tmp_path / "desk")
    save_source(service, "a", saved_item=item())
    repository = service.collection.repository
    before = (repository.sources(), repository.items(), repository.jobs())
    with pytest.raises(CollectionFailure):
        repository.scope_snapshot(selected)
    assert (repository.sources(), repository.items(), repository.jobs()) == before


def test_newer_unrelated_items_cannot_consume_the_selected_case_limit(tmp_path):
    service = workspace(tmp_path / "desk")
    selected = save_source(service, "a", saved_item=item())
    unrelated = save_source(service, "unrelated", saved_item=item("unrelated"))
    repository = service.collection.repository
    later = CLOCK + timedelta(minutes=1)
    repository.enqueue(unrelated, now=later)
    job = repository.claim(now=later)
    repository.complete(
        job,
        CollectedBatch((SourceItem.model_validate(item("newer", published=later.isoformat())),)),
        now=later,
    )
    assert repository.items(limit=1)[0]["native_id"] == "post-newer"
    snapshot = repository.scope_snapshot([selected], limit=1)
    assert snapshot["total_items"] == 1 and snapshot["truncated"] is False
    assert [record["native_id"] for record in snapshot["items"]] == ["post-a"]
    assert snapshot["sources"][0]["included_count"] == 1
    assert preview_scope(snapshot)["unknown_publication_times"] == 1


def test_selected_shared_items_count_once_globally_and_remain_in_each_source(tmp_path):
    service = workspace(tmp_path / "desk")
    shared = item("shared")
    a = save_source(service, "a", saved_item=shared)
    b = save_source(service, "b", saved_item=shared)
    repository = service.collection.repository
    snapshot = repository.scope_snapshot([a, b], limit=1)
    assert snapshot["total_items"] == 1 and len(snapshot["items"]) == 1
    assert snapshot["truncated"] is False
    assert [row["item_count"] for row in snapshot["sources"]] == [1, 1]
    assert [row["included_count"] for row in snapshot["sources"]] == [1, 1]
    assert [row["unknown_publication_times"] for row in snapshot["sources"]] == [1, 1]
    summary = preview_scope(snapshot)
    assert summary["source_count"] == 2 and summary["record_count"] == 1
    assert summary["unknown_publication_times"] == 1
    assert scoped_bundle(repository.scope_snapshot([a])).case.id != scoped_bundle(snapshot).case.id


def test_source_summary_and_items_share_one_read_snapshot_during_a_real_wal_write(
    tmp_path, monkeypatch
):
    service = workspace(tmp_path / "desk")
    source_id = save_source(service, "a", saved_item=item())
    repository = service.collection.repository
    with closing(sqlite3.connect(repository.db_path)) as connection:
        assert connection.execute("PRAGMA journal_mode=WAL").fetchone()[0] == "wal"
    writer = type(repository)(repository.db_path)
    later = CLOCK + timedelta(minutes=1)
    writer.enqueue(source_id, now=later)
    running = writer.claim(now=later)
    original_connection = repository.connection
    committed_during_read = []

    class ReadBoundary:
        def __init__(self, connection):
            self.connection = connection

        def execute(self, *args, **kwargs):
            cursor = self.connection.execute(*args, **kwargs)
            # Trigger a real writer after the first read has established its
            # snapshot, before the repository reads the union of saved items.
            if cursor.description is not None and not committed_during_read:
                committed_during_read.append(True)
                writer.complete(
                    running,
                    CollectedBatch(
                        (
                            SourceItem.model_validate(item()),
                            SourceItem.model_validate(
                                item("concurrent-new", published=later.isoformat())
                            ),
                        )
                    ),
                    now=later,
                )
            return cursor

    @contextmanager
    def intercept_connection(*, write=False):
        with original_connection(write=write) as connection:
            yield connection if write else ReadBoundary(connection)

    monkeypatch.setattr(repository, "connection", intercept_connection)
    during = repository.scope_snapshot([source_id])
    assert committed_during_read == [True]
    assert during["sources"][0]["item_count"] == during["total_items"] == 1
    assert during["sources"][0]["included_count"] == 1
    assert during["sources"][0]["unknown_publication_times"] == 1
    assert during["sources"][0]["last_status"] == "running"
    assert [record["native_id"] for record in during["items"]] == ["post-a"]
    after = writer.scope_snapshot([source_id])
    assert after["sources"][0]["item_count"] == after["total_items"] == 2
    assert after["sources"][0]["last_status"] == "succeeded"
    assert {record["native_id"] for record in after["items"]} == {"post-a", "post-concurrent-new"}
