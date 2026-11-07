"""A selected source can only supply the saved version that it actually observed."""

import hashlib
import json
import sqlite3
from datetime import UTC, datetime, timedelta

import pytest

from evidence_explorer.collection.models import CollectedBatch, CollectionFailure, CollectionSource
from evidence_explorer.collection.persistence import CollectionRepository
from evidence_explorer.config import Settings
from evidence_explorer.review.integrations.packages import export_case_package, import_case_package
from evidence_explorer.review.integrations.sources import SourceItem
from evidence_explorer.review.integrations.workspace import WorkspaceRepository, WorkspaceService

CLOCK = datetime(2035, 1, 2, tzinfo=UTC)


@pytest.fixture(autouse=True)
def no_api_credentials(monkeypatch):
    for name in (
        "X_BEARER_TOKEN",
        "REDDIT_ACCESS_TOKEN",
        "REDDIT_CLIENT_ID",
        "REDDIT_CLIENT_SECRET",
        "REDDIT_USER_AGENT",
    ):
        monkeypatch.delenv(name, raising=False)


@pytest.fixture
def workspace(tmp_path):
    settings = Settings(data_dir=tmp_path / "workspace")
    repository = WorkspaceRepository(settings.db_path)
    repository.initialize()
    return WorkspaceService(repository, settings, tmp_path / "artifacts")


def add_source(repository, key):
    source = CollectionSource(
        id=f"source-{key}",
        platform="news",
        label=f"Feed {key.upper()}",
        locator=f"https://publisher.example/{key}/feed.xml",
        enabled=False,
    )
    repository.add(source, now=CLOCK)
    return source.id


def saved_item(key="shared", *, text="Saved version one.", hour=0, feed="a"):
    return SourceItem(
        platform="news",
        channel_id="publisher",
        channel_name="Public publisher",
        native_id=key,
        text=text,
        source_url=f"https://publisher.example/articles/{key}",
        acquired_at=(CLOCK + timedelta(hours=hour)).isoformat(),
        provenance={
            "method": "rss_content",
            "content_scope": "feed_summary",
            "retrieved_url": f"https://publisher.example/{feed}/feed.xml",
            "feed_url": f"https://publisher.example/{feed}/feed.xml",
        },
    )


def complete(repository, source_id, *items, hour=0):
    clock = CLOCK + timedelta(hours=hour)
    job_id = repository.enqueue(source_id, now=clock)
    job = repository.claim(now=clock)
    assert job and job["id"] == job_id
    repository.complete(job, CollectedBatch(tuple(items)), now=clock)


def source_row(snapshot, source_id):
    return next(row for row in snapshot["sources"] if row["id"] == source_id)


def payload_hash(item):
    encoded = json.dumps(
        item.model_dump(mode="json"), ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
    return hashlib.sha256(encoded.encode()).hexdigest()


def downgrade_membership_to_v2(repository):
    """Model an existing v2 store without inventing source-specific historical payloads."""
    with repository.connection(write=True) as connection:
        connection.execute(
            "CREATE TABLE old_memberships("
            "source_id TEXT NOT NULL REFERENCES collection_sources(id),"
            "item_key TEXT NOT NULL REFERENCES collection_items(item_key),"
            "PRIMARY KEY(source_id,item_key))"
        )
        connection.execute(
            "INSERT INTO old_memberships SELECT source_id,item_key FROM collection_source_items"
        )
        connection.execute("DROP TABLE collection_source_items")
        connection.execute("ALTER TABLE old_memberships RENAME TO collection_source_items")
        connection.execute("DELETE FROM collection_schema WHERE version>2")


def database_dump(repository):
    with repository.connection() as connection:
        return "\n".join(connection.iterdump())


def test_unselected_refresh_cannot_change_selected_source_case_or_exact_provenance(
    workspace, tmp_path
):
    repository = workspace.collection.repository
    source_a, source_b = add_source(repository, "a"), add_source(repository, "b")
    first = saved_item()
    complete(repository, source_a, first)
    before_preview = workspace.preview_collection_case([source_a])
    earlier_case = workspace.open_collection_case(
        [source_a], expected_fingerprint=before_preview["fingerprint"]
    )
    earlier_bundle = workspace.repository.get_bundle(earlier_case)

    complete(
        repository,
        source_b,
        saved_item(text="Changed text observed only by B.", hour=1, feed="b"),
        hour=1,
    )

    assert repository.items()[0]["text"] == "Changed text observed only by B."
    assert repository.items(source_id=source_a) == [first.model_dump(mode="json")]
    selected_snapshot = repository.scope_snapshot([source_a])
    assert selected_snapshot["items"] == [first.model_dump(mode="json")]
    after_preview = workspace.preview_collection_case([source_a])
    assert after_preview == before_preview
    case_id = workspace.open_collection_case(
        [source_a], expected_fingerprint=after_preview["fingerprint"]
    )
    assert case_id == earlier_case
    bundle = workspace.repository.get_bundle(case_id)
    assert bundle == earlier_bundle
    assert len(bundle.records) == 1
    record = bundle.records[0]
    assert record.text == first.text
    assert record.acquired_at == first.acquired_at
    assert record.metadata["provenance"]["retrieved_url"] == first.provenance.retrieved_url
    reopened = import_case_package(export_case_package(bundle), tmp_path / "reopened")
    assert reopened.model_dump() == earlier_bundle.model_dump()


def test_exact_shared_record_is_deduplicated_without_losing_membership(workspace):
    repository = workspace.collection.repository
    source_a, source_b = add_source(repository, "a"), add_source(repository, "b")
    first = saved_item()
    complete(repository, source_a, first)
    complete(repository, source_b, first, hour=1)

    snapshot = repository.scope_snapshot([source_b, source_a])
    assert snapshot["items"] == [first.model_dump(mode="json")]
    assert snapshot["total_items"] == 1
    for source_id in (source_a, source_b):
        row = source_row(snapshot, source_id)
        assert row["item_count"] == row["included_count"] == 1
        assert row["omitted_version_count"] == 0
    assert workspace.preview_collection_case([source_a, source_b])["record_count"] == 1


def test_unchanged_reobservation_preserves_that_sources_first_acquisition(workspace):
    repository = workspace.collection.repository
    source_a, source_b = add_source(repository, "a"), add_source(repository, "b")
    first = saved_item()
    complete(repository, source_a, first)
    # An intervening observation of another version must not change A's equality baseline.
    complete(repository, source_b, saved_item(text="B-only update.", hour=1, feed="b"), hour=1)
    complete(repository, source_a, saved_item(hour=2), hour=2)

    assert repository.items(source_id=source_a) == [first.model_dump(mode="json")]
    snapshot = repository.scope_snapshot([source_a])
    assert snapshot["items"] == [first.model_dump(mode="json")]
    assert (
        source_row(snapshot, source_a)["last_collected_at"]
        == (CLOCK + timedelta(hours=2)).isoformat()
    )


def test_mixed_selected_versions_use_latest_selected_and_explain_omitted_version(workspace):
    repository = workspace.collection.repository
    source_a, source_b, source_c = [add_source(repository, key) for key in ("a", "b", "c")]
    complete(repository, source_a, saved_item())
    selected_latest = saved_item(text="Latest selected observation.", hour=1, feed="b")
    complete(repository, source_b, selected_latest, hour=1)
    complete(
        repository,
        source_c,
        saved_item(
            text="Newest global observation is outside the selected scope.", hour=2, feed="c"
        ),
        hour=2,
    )

    snapshot = repository.scope_snapshot([source_b, source_a])
    assert snapshot["items"] == [selected_latest.model_dump(mode="json")]
    a, b = source_row(snapshot, source_a), source_row(snapshot, source_b)
    assert a["included_count"] == 0 and a["omitted_version_count"] == 1
    assert b["included_count"] == 1 and b["omitted_version_count"] == 0
    forward = workspace.preview_collection_case([source_a, source_b])
    reverse = workspace.preview_collection_case([source_b, source_a])
    assert forward == reverse
    assert any("Feed A" in warning and "version" in warning for warning in forward["limitations"])


def test_bounded_selected_order_uses_its_observations_not_global_last_seen(workspace):
    repository = workspace.collection.repository
    source_a, source_b = add_source(repository, "a"), add_source(repository, "b")
    old = saved_item("old")
    newer = saved_item("newer", hour=1)
    complete(repository, source_a, old)
    complete(repository, source_a, newer, hour=1)
    complete(repository, source_b, saved_item("old", hour=2, feed="b"), hour=2)

    assert repository.items(limit=1)[0]["native_id"] == "old"
    assert repository.items(limit=1, source_id=source_a) == [newer.model_dump(mode="json")]
    snapshot = repository.scope_snapshot([source_a], limit=1)
    assert snapshot["items"] == [newer.model_dump(mode="json")]
    assert snapshot["total_items"] == 2 and snapshot["truncated"] is True
    assert source_row(snapshot, source_a)["included_count"] == 1


def test_v2_migration_preserves_legacy_data_but_never_guesses_source_observation(workspace):
    repository = workspace.collection.repository
    source_a = add_source(repository, "a")
    first = saved_item()
    complete(repository, source_a, first)
    initial_preview = workspace.preview_collection_case([source_a])
    case_id = workspace.open_collection_case(
        [source_a], expected_fingerprint=initial_preview["fingerprint"]
    )
    initial_case = workspace.repository.get_bundle(case_id)
    existing_global_items = repository.items()
    existing_jobs = repository.jobs()
    downgrade_membership_to_v2(repository)

    repository.initialize()
    once = database_dump(repository)
    repository.initialize()
    assert database_dump(repository) == once
    assert repository.items() == existing_global_items
    assert repository.jobs() == existing_jobs
    assert workspace.repository.get_bundle(case_id) == initial_case
    assert repository.items(source_id=source_a) == []
    with repository.connection() as connection:
        versions = {row[0] for row in connection.execute("SELECT version FROM collection_schema")}
        row = connection.execute("SELECT * FROM collection_source_items").fetchone()
    assert versions == {1, 2, 3}
    assert (
        row["payload"]
        is row["semantic_hash"]
        is row["first_seen_at"]
        is row["last_seen_at"]
        is None
    )
    snapshot = repository.scope_snapshot([source_a])
    assert snapshot["items"] == []
    legacy = source_row(snapshot, source_a)
    assert legacy["item_count"] == 1 and legacy["included_count"] == 0
    assert legacy["unattributed_count"] == 1
    preview = workspace.preview_collection_case([source_a])
    assert preview["can_create"] is False
    assert any(
        "Feed A" in warning
        and "not included" in warning
        and ("provenance" in warning.lower() or "attribution" in warning.lower())
        for warning in preview["limitations"]
    )

    # Re-observing now establishes only the new acquisition, not reconstructed history.
    current = saved_item(hour=4)
    complete(repository, source_a, current, hour=4)
    assert repository.items(source_id=source_a) == [current.model_dump(mode="json")]
    assert source_row(repository.scope_snapshot([source_a]), source_a)["unattributed_count"] == 0


def test_legacy_membership_cannot_borrow_new_observation_from_another_selected_source(
    workspace, tmp_path
):
    repository = workspace.collection.repository
    source_a, source_b = add_source(repository, "a"), add_source(repository, "b")
    complete(repository, source_a, saved_item())
    downgrade_membership_to_v2(repository)
    repository.initialize()
    current = saved_item(text="Only B observed this version.", hour=4, feed="b")
    complete(repository, source_b, current, hour=4)

    snapshot = repository.scope_snapshot([source_a, source_b])
    a, b = source_row(snapshot, source_a), source_row(snapshot, source_b)
    assert a["unattributed_count"] == 1 and a["included_count"] == 0
    assert b["unattributed_count"] == 0 and b["included_count"] == 1
    assert snapshot["items"] == [current.model_dump(mode="json")]
    preview = workspace.preview_collection_case([source_a, source_b])
    case_id = workspace.open_collection_case(
        [source_b, source_a], expected_fingerprint=preview["fingerprint"]
    )
    bundle = workspace.repository.get_bundle(case_id)
    reopened = import_case_package(export_case_package(bundle), tmp_path / "legacy-reopened")
    assert reopened.model_dump() == bundle.model_dump()
    persisted = source_row(reopened.case.coverage["collection_scope"], source_a)
    assert persisted["unattributed_count"] == 1 and persisted["included_count"] == 0


def test_omitted_version_details_and_selection_rule_survive_saved_case_and_reopening(
    workspace, tmp_path
):
    repository = workspace.collection.repository
    source_a, source_b = add_source(repository, "a"), add_source(repository, "b")
    old, new = saved_item(), saved_item(text="B's selected version.", hour=1, feed="b")
    complete(repository, source_a, old)
    complete(repository, source_b, new, hour=1)
    preview = workspace.preview_collection_case([source_a, source_b])
    assert preview["version_policy"] == "latest_selected_observation_then_source_id"
    assert len(preview["observations_fingerprint"]) == 64
    omissions = source_row(preview, source_a)["version_omissions"]
    assert len(omissions) == 1
    omitted = omissions[0]
    assert len(omitted["item_key"]) == 64
    assert (omitted["channel_id"], omitted["native_id"]) == ("publisher", "shared")
    assert omitted["observed_at"] == CLOCK.isoformat()
    assert omitted["payload_hash"] == payload_hash(old)
    assert omitted["content_hash"] == hashlib.sha256(old.text.encode()).hexdigest()
    assert omitted["included_source_id"] == source_b
    assert omitted["included_observed_at"] == (CLOCK + timedelta(hours=1)).isoformat()
    assert omitted["included_payload_hash"] == payload_hash(new)
    assert omitted["included_content_hash"] == hashlib.sha256(new.text.encode()).hexdigest()
    assert omitted["observation_time_tied"] is False
    assert omitted["included_in_case"] is True
    assert omitted["reason"]
    case_id = workspace.open_collection_case(
        [source_b, source_a], expected_fingerprint=preview["fingerprint"]
    )
    saved = workspace.repository.get_bundle(case_id)
    reopened = import_case_package(export_case_package(saved), tmp_path / "version-reopened")
    saved_scope = reopened.case.coverage["collection_scope"]
    assert saved_scope["version_policy"] == preview["version_policy"]
    assert saved_scope["observations_fingerprint"] == preview["observations_fingerprint"]
    assert source_row(saved_scope, source_a)["version_omissions"] == omissions
    assert reopened.model_dump() == saved.model_dump()


def test_omitted_observation_change_invalidates_preview_without_changing_included_payload(
    workspace,
):
    repository = workspace.collection.repository
    source_a, source_b = add_source(repository, "a"), add_source(repository, "b")
    complete(repository, source_a, saved_item())
    complete(
        repository,
        source_b,
        saved_item(text="B's selected version.", hour=1, feed="b"),
        hour=1,
    )
    preview = workspace.preview_collection_case([source_a, source_b])
    old_case_id = workspace.open_collection_case(
        [source_a, source_b], expected_fingerprint=preview["fingerprint"]
    )
    old_bundle = workspace.repository.get_bundle(old_case_id)
    snapshot = repository.scope_snapshot([source_a, source_b])
    changed = saved_item(text="Different omitted observation, still older than B.")
    # Modify only the omitted observation, leaving source/job clocks and the winner unchanged.
    with repository.connection(write=True) as connection:
        connection.execute(
            "UPDATE collection_source_items SET payload=?,semantic_hash=? WHERE source_id=?",
            (changed.model_dump_json(), payload_hash(changed), source_a),
        )
    after_snapshot = repository.scope_snapshot([source_a, source_b])
    assert after_snapshot["items"] == snapshot["items"]
    assert (
        source_row(after_snapshot, source_a)["last_collected_at"]
        == source_row(snapshot, source_a)["last_collected_at"]
    )
    assert after_snapshot["observations_fingerprint"] != snapshot["observations_fingerprint"]
    after_preview = workspace.preview_collection_case([source_a, source_b])
    assert after_preview["fingerprint"] != preview["fingerprint"]
    with pytest.raises(CollectionFailure, match="changed"):
        workspace.open_collection_case(
            [source_a, source_b], expected_fingerprint=preview["fingerprint"]
        )
    assert workspace.repository.get_bundle(old_case_id) == old_bundle


@pytest.mark.parametrize(
    ("platform", "locator", "credential_name"),
    [("x", "public_source", "X_BEARER_TOKEN"), ("reddit", "worldnews", "REDDIT_ACCESS_TOKEN")],
)
def test_access_setup_is_separate_from_history_and_persisted_without_credentials(
    workspace, tmp_path, monkeypatch, platform, locator, credential_name
):
    repository = workspace.collection.repository
    public_id = add_source(repository, "public")
    complete(repository, public_id, saved_item())
    api_id = workspace.add_collection_source(
        {"platform": platform, "locator": locator, "label": "API source", "enabled": False}
    )
    before_jobs = repository.jobs()
    preview = workspace.preview_collection_case([public_id, api_id])
    unavailable = source_row(preview, api_id)
    assert unavailable["ready"] is False
    assert unavailable["last_status"] == "idle"
    assert "Connect" in unavailable["connection_status"]
    assert unavailable["item_count"] == unavailable["included_count"] == 0
    old_case = workspace.open_collection_case(
        [api_id, public_id], expected_fingerprint=preview["fingerprint"]
    )
    bundle = workspace.repository.get_bundle(old_case)
    reopened = import_case_package(export_case_package(bundle), tmp_path / f"{platform}-reopened")
    assert source_row(reopened.case.coverage["collection_scope"], api_id) == unavailable

    secret = f"fixture-only-{platform}-access-token-do-not-save"
    monkeypatch.setenv(credential_name, secret)
    connected = workspace.preview_collection_case([api_id, public_id])
    connected_row = source_row(connected, api_id)
    assert connected_row["ready"] is True
    assert connected_row["last_status"] == "idle"
    assert "configured" in connected_row["connection_status"]
    assert connected["fingerprint"] != preview["fingerprint"]
    with pytest.raises(CollectionFailure, match="changed"):
        workspace.open_collection_case(
            [api_id, public_id], expected_fingerprint=preview["fingerprint"]
        )
    connected_case = workspace.open_collection_case(
        [public_id, api_id], expected_fingerprint=connected["fingerprint"]
    )
    connected_bundle = workspace.repository.get_bundle(connected_case)
    assert source_row(connected_bundle.case.coverage["collection_scope"], api_id)["ready"] is True
    assert source_row(bundle.case.coverage["collection_scope"], api_id)["ready"] is False
    assert secret not in json.dumps(connected)
    assert secret not in connected_bundle.model_dump_json()
    assert secret not in database_dump(repository)
    assert repository.jobs() == before_jobs


@pytest.mark.parametrize(
    ("platform", "method", "content_scope", "label"),
    [
        ("news", "rss_content", "feed_summary", "feed summaries"),
        ("youtube", "youtube_feed", "title_description", "video titles and descriptions"),
    ],
)
def test_saved_content_scope_survives_failed_refresh_and_package_reopening(
    workspace, tmp_path, platform, method, content_scope, label
):
    repository = workspace.collection.repository
    locator = "https://publisher.example/news/feed.xml" if platform == "news" else "UC" + "a" * 22
    config = CollectionSource(
        id="content-source",
        platform=platform,
        label="Content source",
        locator=locator,
        enabled=False,
    )
    repository.add(config, now=CLOCK)
    item = saved_item().model_copy(
        update={
            "platform": platform,
            "provenance": saved_item().provenance.model_copy(
                update={"method": method, "content_scope": content_scope}
            ),
        }
    )
    complete(repository, config.id, item)
    before = workspace.preview_collection_case([config.id])
    earlier_case = workspace.open_collection_case(
        [config.id], expected_fingerprint=before["fingerprint"]
    )
    earlier_bundle = workspace.repository.get_bundle(earlier_case)
    clock = CLOCK + timedelta(hours=1)
    repository.enqueue(config.id, now=clock)
    job = repository.claim(now=clock)
    assert job
    repository.fail(job, "Fixture refresh failure replaced the latest status text.", now=clock)

    snapshot = repository.scope_snapshot([config.id])
    assert snapshot["items"] == [item.model_dump(mode="json")]
    assert source_row(snapshot, config.id)["content_scopes"] == {content_scope: 1}
    preview = workspace.preview_collection_case([config.id])
    selected = source_row(preview, config.id)
    assert selected["last_status"] == "failed" and selected["ready"] is True
    assert selected["content_scopes"] == {content_scope: 1}
    assert any(label in limitation for limitation in preview["limitations"])
    after_case = workspace.open_collection_case(
        [config.id], expected_fingerprint=preview["fingerprint"]
    )
    after_bundle = workspace.repository.get_bundle(after_case)
    assert after_bundle.records == earlier_bundle.records
    assert workspace.repository.get_bundle(earlier_case) == earlier_bundle
    reopened = import_case_package(
        export_case_package(after_bundle), tmp_path / f"{content_scope}-reopened"
    )
    saved_scope = reopened.case.coverage["collection_scope"]
    assert source_row(saved_scope, config.id)["content_scopes"] == {content_scope: 1}
    assert any(label in limitation for limitation in saved_scope["limitations"])
    assert reopened.records[0].metadata["provenance"]["content_scope"] == content_scope


def test_equal_observation_times_use_explicit_source_id_tie_convention(workspace):
    repository = workspace.collection.repository
    source_a, source_b = add_source(repository, "a"), add_source(repository, "b")
    old = saved_item(text="Source A's selected observation.")
    other = saved_item(text="Source B's different observation.", hour=1, feed="b")
    complete(repository, source_a, old, hour=1)
    complete(repository, source_b, other, hour=1)
    with repository.connection(write=True) as connection:
        connection.execute(
            "UPDATE collection_source_items SET last_seen_at=? WHERE source_id=?",
            ("2035-01-02T02:00:00+01:00", source_b),
        )

    snapshot = repository.scope_snapshot([source_b, source_a])
    assert snapshot["items"] == [old.model_dump(mode="json")]
    omitted = source_row(snapshot, source_b)["version_omissions"][0]
    assert omitted["observation_time_tied"] is True
    assert omitted["included_source_id"] == source_a
    assert "tie" in omitted["reason"].lower()
    assert workspace.preview_collection_case([source_a, source_b]) == (
        workspace.preview_collection_case([source_b, source_a])
    )


def test_observation_recency_normalizes_offsets_instead_of_sorting_timestamp_text(workspace):
    repository = workspace.collection.repository
    source_a, source_b = add_source(repository, "a"), add_source(repository, "b")
    old = saved_item(text="A is earlier in UTC.")
    newer = saved_item(text="B is later in UTC.", hour=1, feed="b")
    complete(repository, source_a, old)
    complete(repository, source_b, newer, hour=1)
    with repository.connection(write=True) as connection:
        connection.execute(
            "UPDATE collection_source_items SET last_seen_at=? WHERE source_id=?",
            ("2035-01-02T03:00:00+02:00", source_a),
        )
        connection.execute(
            "UPDATE collection_source_items SET last_seen_at=? WHERE source_id=?",
            ("2035-01-02T01:30:00+00:00", source_b),
        )
    snapshot = repository.scope_snapshot([source_a, source_b])
    assert snapshot["items"] == [newer.model_dump(mode="json")]
    omission = source_row(snapshot, source_a)["version_omissions"][0]
    assert omission["observation_time_tied"] is False


@pytest.mark.parametrize("timestamp", [None, "not-a-time", "2035-01-02T01:00:00"])
def test_known_payload_without_valid_aware_observation_time_cannot_create_scope(
    workspace, timestamp
):
    repository = workspace.collection.repository
    source_a = add_source(repository, "a")
    complete(repository, source_a, saved_item())
    with repository.connection(write=True) as connection:
        connection.execute(
            "UPDATE collection_source_items SET last_seen_at=? WHERE source_id=?",
            (timestamp, source_a),
        )
    with pytest.raises(CollectionFailure):
        workspace.preview_collection_case([source_a])


def test_earlier_completion_cannot_overwrite_any_saved_observation_or_job(workspace):
    repository = workspace.collection.repository
    source_a = add_source(repository, "a")
    complete(repository, source_a, saved_item(hour=2), hour=2)
    queue_time = CLOCK + timedelta(hours=3)
    repository.enqueue(source_a, now=queue_time)
    job = repository.claim(now=queue_time)
    assert job
    before = database_dump(repository)
    with pytest.raises(ValueError):
        repository.complete(
            job,
            CollectedBatch(
                (
                    saved_item("new-native-id", text="No partial insert may survive.", hour=1),
                    saved_item(
                        text="An older completed observation must not replace newer.", hour=1
                    ),
                )
            ),
            now=CLOCK + timedelta(hours=1),
        )
    assert database_dump(repository) == before


def test_v3_migration_failure_rolls_back_columns_versions_and_existing_data(workspace):
    repository = workspace.collection.repository
    source_a = add_source(repository, "a")
    complete(repository, source_a, saved_item())
    downgrade_membership_to_v2(repository)
    with repository.connection(write=True) as connection:
        connection.execute(
            "CREATE TRIGGER block_collection_v3 BEFORE INSERT ON collection_schema "
            "WHEN NEW.version=3 BEGIN SELECT RAISE(ABORT, 'test migration blocked'); END"
        )
    before = database_dump(repository)
    with pytest.raises(sqlite3.IntegrityError, match="test migration blocked"):
        repository.initialize()
    assert database_dump(repository) == before


def test_future_collection_version_is_rejected_without_altering_store(tmp_path):
    path = tmp_path / "future.sqlite3"
    repository = CollectionRepository(path)
    with repository.connection(write=True) as connection:
        connection.execute("CREATE TABLE collection_schema(version INTEGER PRIMARY KEY)")
        connection.execute("INSERT INTO collection_schema VALUES (99)")
        connection.execute("CREATE TABLE future_marker(payload TEXT)")
        connection.execute("INSERT INTO future_marker VALUES (?)", (json.dumps({"keep": True}),))
    before = database_dump(repository)
    with pytest.raises(ValueError, match="newer schema version"):
        repository.initialize()
    assert database_dump(repository) == before
