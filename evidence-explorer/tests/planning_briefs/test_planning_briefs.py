"""Planning is revisioned intent, isolated from collection and execution."""

import hashlib
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime

import pytest

from evidence_explorer.persistence.migrations import Migration, SchemaVersionError
from evidence_explorer.planning_briefs import schema
from evidence_explorer.planning_briefs.models import (
    BriefRevision,
    DraftFields,
    PlanningHistory,
    revision_content_hash,
)
from evidence_explorer.planning_briefs.persistence import PlanningBriefRepository
from evidence_explorer.planning_briefs.scope import scope_identity, validate_history_for_bundle
from evidence_explorer.planning_briefs.service import PlanningBriefService
from evidence_explorer.review.contracts import EvidenceBundle
from evidence_explorer.review.storage import ReviewRepository, VersionConflictError


def bundle(case_id="case-a", *, label="Saved source"):
    text = "The supplied public record says the meeting happened."
    return EvidenceBundle.model_validate(
        {
            "case": {"id": case_id, "title": "Saved case", "question": "What is supported?"},
            "fragments": [],
            "findings": [],
            "records": [
                {
                    "id": f"{case_id}-record",
                    "kind": "article",
                    "source_label": label,
                    "source_url": "https://example.org/saved",
                    "text": text,
                    "content_hash": hashlib.sha256(text.encode()).hexdigest(),
                }
            ],
            "runs": [
                {
                    "id": f"{case_id}-run",
                    "input_fingerprint": "saved-input",
                    "input_manifest": [f"{case_id}-record"],
                    "method": "manual",
                    "method_version": "1",
                    "created_at": "2026-11-01T00:00:00Z",
                }
            ],
        }
    )


@pytest.fixture
def desk(tmp_path):
    reviews = ReviewRepository(tmp_path / "review.sqlite3")
    reviews.initialize()
    reviews.import_bundle(bundle())
    reviews.import_bundle(bundle("case-b"))
    repository = PlanningBriefRepository(
        reviews._connection, clock=lambda: datetime(2026, 11, 7, tzinfo=UTC)
    )
    repository.initialize()
    return reviews, PlanningBriefService(repository)


def fields(case_id="case-a", **changes):
    return DraftFields.model_validate(
        {
            "use_case_id": "cited_claim_investigation",
            "purpose": "Review the supplied statement",
            "public_subjects_or_topics": ["Public meeting"],
            "source_selection": [f"{case_id}-record"],
            "reviewer_role": "Analyst",
            "expected_output": "reviewed_brief",
            **changes,
        }
    )


def bound(desk, *, draft_fields=None):
    reviews, service = desk
    draft = service.create_draft(author="Mina", fields=draft_fields or fields())
    return service.bind(
        draft.brief_id,
        "case-a",
        expected_head_revision_id=draft.revision_id,
        expected_case_fingerprint=reviews.get_case_fingerprint("case-a"),
        author="Mina",
    )


def confirmed(desk):
    reviews, service = desk
    revision = bound(desk)
    confirmation = service.confirm(
        revision.brief_id,
        expected_head_revision_id=revision.revision_id,
        expected_case_fingerprint=reviews.get_case_fingerprint("case-a"),
        reviewer="Mina",
    )
    return revision, confirmation


def counts(reviews):
    with reviews._connection() as connection:
        return {
            table: connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            for table in ("review_cases", "review_jobs", "review_scenarios", "review_events")
        }


def test_empty_draft_is_persisted_unbound_without_execution_side_effects(desk):
    reviews, service = desk
    before = counts(reviews)
    draft = service.create_draft(author="Mina")
    assert service.load(draft.brief_id).status == "draft"
    assert draft.fields == DraftFields()
    assert draft.binding is None
    assert service.state_for_case("case-a") is None
    assert counts(reviews) == before
    assert service.list()[0].revision == draft


def test_confirm_and_edit_preserve_immutable_history_and_need_reconfirmation(desk):
    reviews, service = desk
    before = counts(reviews)
    revision, confirmation = confirmed(desk)
    assert service.load(revision.brief_id).status == "confirmed"
    edited = service.save_draft(
        revision.brief_id,
        fields(purpose="A revised public question"),
        base_revision_id=revision.revision_id,
        expected_head_revision_id=revision.revision_id,
        author="Mina",
    )
    current = service.load(revision.brief_id)
    assert current.status == "needs_review"
    assert current.confirmation is None
    assert edited.binding == revision.binding
    assert service.load(revision.brief_id, revision.revision_id).confirmation == confirmation
    assert not service.load(revision.brief_id, revision.revision_id).is_head
    assert counts(reviews) == before
    with reviews._connection(write=True) as connection:
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            connection.execute("UPDATE monitoring_brief_revisions SET number=99")


def test_historical_base_cannot_silently_save_against_current_head(desk):
    _, service = desk
    first = service.create_draft(author="Mina")
    latest = service.save_draft(
        first.brief_id,
        fields(),
        base_revision_id=first.revision_id,
        expected_head_revision_id=first.revision_id,
        author="Mina",
    )
    with pytest.raises(VersionConflictError, match="historical"):
        service.save_draft(
            first.brief_id,
            fields(),
            base_revision_id=first.revision_id,
            expected_head_revision_id=latest.revision_id,
            author="Mina",
        )
    assert service.load(first.brief_id).revision == latest


def test_two_editors_cannot_overwrite_each_other(desk):
    _, service = desk
    first = service.create_draft(author="Mina")

    def save(purpose):
        try:
            return service.save_draft(
                first.brief_id,
                fields(purpose=purpose),
                base_revision_id=first.revision_id,
                expected_head_revision_id=first.revision_id,
                author="Mina",
            )
        except VersionConflictError:
            return None

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(save, ("First edit", "Second edit")))
    assert sum(result is not None for result in results) == 1
    assert len(service.history(first.brief_id).revisions) == 2


def test_case_fingerprint_is_checked_before_binding_or_head_change(desk):
    _, service = desk
    draft = service.create_draft(author="Mina", fields=fields())
    with pytest.raises(VersionConflictError):
        service.bind(
            draft.brief_id,
            "case-a",
            expected_head_revision_id=draft.revision_id,
            expected_case_fingerprint="0" * 64,
            author="Mina",
        )
    assert service.load(draft.brief_id).revision == draft
    assert service.state_for_case("case-a") is None


def test_two_cases_have_independent_active_plans(desk):
    reviews, service = desk
    first, _ = confirmed(desk)
    draft = service.create_draft(author="Mina", fields=fields("case-b"))
    second = service.bind(
        draft.brief_id,
        "case-b",
        expected_head_revision_id=draft.revision_id,
        expected_case_fingerprint=reviews.get_case_fingerprint("case-b"),
        author="Mina",
    )
    assert service.state_for_case("case-a").revision == first
    assert service.state_for_case("case-b").revision == second
    with pytest.raises(ValueError, match="another case"):
        service.bind(
            first.brief_id,
            "case-b",
            expected_head_revision_id=first.revision_id,
            expected_case_fingerprint=reviews.get_case_fingerprint("case-b"),
            author="Mina",
        )


def test_binding_requires_exact_whole_saved_record_manifest(desk):
    reviews, service = desk
    draft = service.create_draft(author="Mina", fields=fields(source_selection=("a guess",)))
    with pytest.raises(ValueError, match="exactly"):
        service.bind(
            draft.brief_id,
            "case-a",
            expected_head_revision_id=draft.revision_id,
            expected_case_fingerprint=reviews.get_case_fingerprint("case-a"),
            author="Mina",
        )
    identity = scope_identity(bundle())
    assert identity.basis == "saved_evidence_manifest"
    assert identity.source_refs == ("case-a-record",)
    assert identity.collection_scope_fingerprint is None
    assert identity.source_observations == ()


def test_history_only_changes_preserve_freshness_but_evidence_changes_do_not(desk):
    reviews, service = desk
    revision, _ = confirmed(desk)
    history = bundle().model_dump(mode="json")
    history["scenarios"] = [{"id": "history", "base_run_id": "case-a-run"}]
    reviews.import_bundle(EvidenceBundle.model_validate(history))
    assert reviews.get_case_fingerprint("case-a") != revision.binding.source_case_fingerprint
    assert service.load(revision.brief_id).status == "confirmed"
    newer = bundle().model_dump(mode="json")
    newer["runs"][0]["id"] = "new-analysis"
    reviews.import_bundle(EvidenceBundle.model_validate(newer))
    assert service.load(revision.brief_id).status == "needs_review"
    with pytest.raises(VersionConflictError, match="bound evidence"):
        service.confirm(
            revision.brief_id,
            expected_head_revision_id=revision.revision_id,
            expected_case_fingerprint=reviews.get_case_fingerprint("case-a"),
            reviewer="Mina",
        )


def test_incomplete_scope_and_required_windows_cannot_be_confirmed(desk):
    reviews, service = desk
    revision = bound(desk, draft_fields=fields(use_case_id="public_information_monitoring"))
    with pytest.raises(ValueError, match="windows"):
        service.confirm(
            revision.brief_id,
            expected_head_revision_id=revision.revision_id,
            expected_case_fingerprint=reviews.get_case_fingerprint("case-a"),
            reviewer="Mina",
        )
    assert service.load(revision.brief_id).status == "needs_review"


def test_unknown_and_naive_windows_stay_distinct():
    assert fields(baseline_window={"id": "baseline"}).baseline_window.start is None
    with pytest.raises(ValueError, match="timezone"):
        fields(baseline_window={"id": "baseline", "start": "2026-11-01T00:00:00"})


def test_equal_count_rules_compare_real_utc_duration():
    rule = {
        "id": "r",
        "title": "Review",
        "metric": "saved records",
        "threshold_percent": 20,
        "threshold_basis": "analyst_configured",
        "direction": "increase",
        "comparison_basis": "equal_duration_counts",
        "min_baseline_count": 1,
        "min_current_count": 0,
        "min_coverage_fraction": 0.5,
    }
    with pytest.raises(ValueError, match="equal UTC"):
        fields(
            alert_rule=rule,
            baseline_window={
                "id": "b",
                "start": "2026-10-25T01:00:00+02:00",
                "end": "2026-10-25T03:00:00+01:00",
            },
            observation_window={
                "id": "o",
                "start": "2026-10-26T01:00:00+01:00",
                "end": "2026-10-26T03:00:00+01:00",
            },
        )


def test_import_is_idempotent_historical_visible_and_never_activates(tmp_path, desk):
    _, source = desk
    revision, original_confirmation = confirmed(desk)
    history = source.history_for_case("case-a")
    target_reviews = ReviewRepository(tmp_path / "target.sqlite3")
    target_reviews.initialize()
    target_reviews.import_bundle(bundle())
    repository = PlanningBriefRepository(target_reviews._connection)
    repository.initialize()
    target = PlanningBriefService(repository)
    target.import_history(history)
    token = target.context_token_for_case("case-a")
    target.import_history(history)
    assert target.context_token_for_case("case-a") == token
    imported = target.load(revision.brief_id)
    assert imported.imported and imported.head_revision_id is None
    assert imported.confirmation == original_confirmation
    assert not imported.confirmation_is_local
    assert imported.status == "needs_review"
    assert target.list()[0] == imported
    assert target.history_for_case("case-a") == history
    with target_reviews._connection() as connection:
        assert (
            connection.execute("SELECT COUNT(*) FROM monitoring_brief_active_cases").fetchone()[0]
            == 0
        )
    adopted = target.adopt_revision(
        revision.revision_id,
        "case-a",
        expected_head_revision_id=None,
        expected_case_fingerprint=target_reviews.get_case_fingerprint("case-a"),
        author="Jo",
    )
    assert adopted.number == revision.number + 1
    assert target.load(revision.brief_id).confirmation is None
    assert target.load(revision.brief_id).status == "needs_review"
    assert target.context_token_for_case("case-a") != token


def test_import_different_bytes_same_identity_is_atomic(desk):
    _, service = desk
    revision, _ = confirmed(desk)
    changed = BriefRevision.model_validate({**revision.model_dump(mode="json"), "author": "Other"})
    before = service.context_token_for_case("case-a")
    with pytest.raises(ValueError, match="different content"):
        service.import_history(PlanningHistory(revisions=(changed,)))
    assert service.context_token_for_case("case-a") == before


def test_complete_transport_rejects_missing_ancestors_and_unrelated_cases(desk):
    _, service = desk
    revision, _ = confirmed(desk)
    with pytest.raises(ValueError, match="parent"):
        validate_history_for_bundle(PlanningHistory(revisions=(revision,)), bundle())
    history = service.history_for_case("case-a")
    with pytest.raises(ValueError, match="another case"):
        validate_history_for_bundle(history, bundle("case-b"))
    validate_history_for_bundle(history, bundle())


def test_context_token_changes_when_active_brief_switches(desk):
    _, service = desk
    first, _ = confirmed(desk)
    before = service.context_token_for_case("case-a")
    second = bound(desk)
    assert service.context_token_for_case("case-a") != before
    assert service.state_for_case("case-a").revision == second
    assert service.load(first.brief_id).status == "needs_review"


def test_revision_payload_bound_rejects_large_binding_without_head_change(desk):
    reviews, service = desk
    large = bundle("large", label="x" * (1024 * 1024))
    reviews.import_bundle(large)
    draft = service.create_draft(author="Mina", fields=fields("large"))
    with pytest.raises(ValueError, match="1 MiB"):
        service.bind(
            draft.brief_id,
            "large",
            expected_head_revision_id=draft.revision_id,
            expected_case_fingerprint=large.fingerprint(),
            author="Mina",
        )
    assert service.load(draft.brief_id).revision == draft


def test_history_export_refuses_truncation_but_single_brief_pages_are_explicit(desk):
    _, service = desk
    revision, _ = confirmed(desk)
    assert len(service.history(revision.brief_id, limit=1).revisions) == 1
    with pytest.raises(ValueError, match="revision limit"):
        service.history_for_case("case-a", limit=1)


def test_component_migration_is_idempotent_and_rejects_future_version(desk):
    reviews, service = desk
    service.repository.initialize()
    with reviews._connection(write=True) as connection:
        connection.execute(
            "INSERT INTO schema_migrations VALUES ('monitoring_briefs',2,'future','2026-11-07')"
        )
    with pytest.raises(SchemaVersionError):
        service.repository.initialize()


def test_failed_component_migration_rolls_back_ddl_and_journal(tmp_path, monkeypatch):
    reviews = ReviewRepository(tmp_path / "migration.sqlite3")
    reviews.initialize()

    def broken(connection):
        connection.execute("CREATE TABLE brief_must_rollback(id INTEGER)")
        raise RuntimeError("failure")

    monkeypatch.setattr(schema, "MIGRATIONS", (Migration(1, "broken", broken),))
    with pytest.raises(RuntimeError, match="failure"):
        PlanningBriefRepository(reviews._connection).initialize()
    with reviews._connection() as connection:
        assert (
            connection.execute(
                "SELECT 1 FROM sqlite_master WHERE name='brief_must_rollback'"
            ).fetchone()
            is None
        )
        assert (
            connection.execute(
                "SELECT 1 FROM schema_migrations WHERE component='monitoring_briefs'"
            ).fetchone()
            is None
        )


def test_tampered_scope_hash_is_rejected_even_for_historical_binding(desk):
    _, service = desk
    revision, _ = confirmed(desk)
    changed = revision.model_dump(mode="json")
    changed["binding"]["scope"]["scope_fingerprint"] = "0" * 64
    with pytest.raises(ValueError, match="scope identity hash"):
        BriefRevision.model_validate(changed)


def test_confirmation_digest_must_match_exact_revision(desk):
    _, service = desk
    _, confirmation = confirmed(desk)
    history = service.history_for_case("case-a")
    changed = confirmation.model_copy(update={"binding_digest": "0" * 64})
    with pytest.raises(ValueError, match="binding digest"):
        validate_history_for_bundle(
            history.model_copy(update={"confirmations": (changed,)}), bundle()
        )


def test_no_client_confirmation_flag_or_mutable_revision_payload():
    with pytest.raises(ValueError, match="Extra inputs"):
        DraftFields.model_validate({"confirmed": True})
    current = fields()
    with pytest.raises(ValueError, match="frozen"):
        current.purpose = "Mutated"


def test_source_model_revalidation_catches_mutated_revision_instances(desk):
    _, service = desk
    revision, _ = confirmed(desk)
    changed_fields = fields(purpose="Changed after hashing")
    forged = revision.model_copy(update={"fields": changed_fields})
    assert revision_content_hash(changed_fields, revision.binding) != revision.content_hash
    with pytest.raises(ValueError, match="content hash"):
        service.import_history(
            PlanningHistory.model_construct(revisions=(forged,), confirmations=())
        )


def test_saved_collection_scope_includes_failed_empty_sources_and_ignores_live_changes(desk):
    from evidence_explorer.collection.models import CollectedBatch, CollectionSource
    from evidence_explorer.collection.persistence import CollectionRepository
    from evidence_explorer.collection.scoping import scoped_bundle
    from evidence_explorer.review.integrations.sources import SourceItem

    reviews, service = desk
    collection = CollectionRepository(reviews.db_path)
    collection.initialize()
    clock = datetime(2026, 11, 1, tzinfo=UTC)
    for name in ("observed", "failed"):
        collection.add(
            CollectionSource(
                id=name,
                platform="news",
                label=name,
                locator=f"https://example.org/{name}.rss",
                enabled=False,
            ),
            now=clock,
        )
    collection.enqueue("observed", now=clock)
    job = collection.claim(now=clock)
    collection.complete(
        job,
        CollectedBatch(
            (
                SourceItem(
                    platform="news",
                    channel_id="publisher",
                    channel_name="Publisher",
                    native_id="item-1",
                    text="The public meeting was held.",
                    acquired_at=clock.isoformat(),
                    source_url="https://example.org/article",
                ),
            )
        ),
        now=clock,
    )
    collection.enqueue("failed", now=clock)
    collection.fail(collection.claim(now=clock), "The public feed was unavailable.", now=clock)
    saved = scoped_bundle(collection.scope_snapshot(["observed", "failed"]))
    reviews.import_bundle(saved)
    identity = scope_identity(saved)
    assert identity.source_refs == ("failed", "observed")
    assert len(identity.source_observations) == 1
    assert identity.source_observations[0].source_id == "observed"
    draft = service.create_draft(author="Mina", fields=fields(source_selection=("observed",)))
    with pytest.raises(ValueError, match="exactly"):
        service.bind(
            draft.brief_id,
            saved.case.id,
            expected_head_revision_id=draft.revision_id,
            expected_case_fingerprint=saved.fingerprint(),
            author="Mina",
        )
    edited = service.save_draft(
        draft.brief_id,
        fields(source_selection=identity.source_refs),
        base_revision_id=draft.revision_id,
        expected_head_revision_id=draft.revision_id,
        author="Mina",
    )
    revision = service.bind(
        draft.brief_id,
        saved.case.id,
        expected_head_revision_id=edited.revision_id,
        expected_case_fingerprint=saved.fingerprint(),
        author="Mina",
    )
    service.confirm(
        revision.brief_id,
        expected_head_revision_id=revision.revision_id,
        expected_case_fingerprint=saved.fingerprint(),
        reviewer="Mina",
    )
    collection.set_enabled("observed", True, now=clock)
    assert service.load(revision.brief_id).status == "confirmed"
    assert service.load(revision.brief_id).revision.binding.scope == identity


def test_historical_scope_must_agree_with_retained_collection_json(desk):
    from evidence_explorer.planning_briefs.models import ScopeIdentity, digest

    identity = scope_identity(bundle()).model_dump(mode="json")
    identity.update(
        basis="collection_scope",
        source_refs=["different"],
        collection_scope_fingerprint="1" * 64,
        observations_fingerprint="2" * 64,
        collection_scope_json='{"source_ids":["original"],"sources":[],"observation_manifest":[]}',
    )
    identity["scope_fingerprint"] = digest(
        {k: v for k, v in identity.items() if k != "scope_fingerprint"}
    )
    with pytest.raises(ValueError, match="exact saved source manifest"):
        ScopeIdentity.model_validate(identity)


def test_list_does_not_read_case_bodies(desk, monkeypatch):
    _, service = desk
    revision, _ = confirmed(desk)

    def forbidden(*args, **kwargs):
        raise AssertionError("List must not deserialize saved case bodies")

    monkeypatch.setattr(service.repository, "_case_binding", forbidden)
    assert service.list()[0].revision == revision


def test_imported_history_does_not_replace_existing_local_head_or_active_plan(desk):
    _, service = desk
    original, _ = confirmed(desk)
    original_history = service.history_for_case("case-a")
    latest = service.save_draft(
        original.brief_id,
        fields(purpose="Locally revised scope"),
        base_revision_id=original.revision_id,
        expected_head_revision_id=original.revision_id,
        author="Mina",
    )
    token = service.context_token_for_case("case-a")
    service.import_history(original_history)
    assert service.load(original.brief_id).revision == latest
    assert service.state_for_case("case-a").revision == latest
    assert service.context_token_for_case("case-a") == token


def test_complete_baseline_scope_can_confirm_intent_without_collection_or_model(desk):
    reviews, service = desk
    complete = fields(
        use_case_id="public_information_monitoring",
        baseline_window={
            "id": "baseline",
            "start": "2026-11-01T00:00:00Z",
            "end": "2026-11-02T00:00:00Z",
        },
        observation_window={
            "id": "observation",
            "start": "2026-11-02T00:00:00Z",
            "end": "2026-11-03T00:00:00Z",
        },
    )
    revision = bound(desk, draft_fields=complete)
    before = counts(reviews)
    first = service.confirm(
        revision.brief_id,
        expected_head_revision_id=revision.revision_id,
        expected_case_fingerprint=reviews.get_case_fingerprint("case-a"),
        reviewer="Mina",
    )
    repeated = service.confirm(
        revision.brief_id,
        expected_head_revision_id=revision.revision_id,
        expected_case_fingerprint=reviews.get_case_fingerprint("case-a"),
        reviewer="Mina",
    )
    assert first == repeated
    assert counts(reviews) == before
    assert service.load(revision.brief_id).status == "confirmed"


def test_alert_rule_without_comparison_windows_remains_unconfirmed_intent(desk):
    reviews, service = desk
    rule = {
        "id": "r",
        "title": "Review",
        "metric": "saved records",
        "threshold_percent": 20,
        "threshold_basis": "analyst_configured",
        "direction": "increase",
        "comparison_basis": "equal_duration_counts",
        "min_baseline_count": 1,
        "min_current_count": 0,
        "min_coverage_fraction": 0.5,
    }
    revision = bound(desk, draft_fields=fields(alert_rule=rule))
    with pytest.raises(ValueError, match="comparison windows"):
        service.confirm(
            revision.brief_id,
            expected_head_revision_id=revision.revision_id,
            expected_case_fingerprint=reviews.get_case_fingerprint("case-a"),
            reviewer="Mina",
        )


def test_history_byte_budget_fails_instead_of_returning_a_partial_export(desk, monkeypatch):
    import evidence_explorer.planning_briefs.persistence as persistence

    _, service = desk
    confirmed(desk)
    monkeypatch.setattr(persistence, "MAX_HISTORY_BYTES", 1024)
    with pytest.raises(ValueError, match="byte budget"):
        service.history_for_case("case-a")


def bind_named(desk, brief_id, case_id):
    reviews, service = desk
    draft = service.create_draft(author="Mina", brief_id=brief_id, fields=fields(case_id))
    return service.bind(
        brief_id,
        case_id,
        expected_head_revision_id=draft.revision_id,
        expected_case_fingerprint=reviews.get_case_fingerprint(case_id),
        author="Mina",
    )


def test_case_filter_precedes_pagination_with_51_unrelated_bound_drafts(desk):
    _, service = desk
    for index in range(51):
        bind_named(desk, f"a-unrelated-{index:02}", "case-b")
    selected = bind_named(desk, "z-selected-case", "case-a")
    unbound = service.create_draft(author="Mina", brief_id="a-unbound")
    assert selected.brief_id not in {state.revision.brief_id for state in service.list()}
    visible = service.list_for_case("case-a")
    assert [state.revision for state in visible] == [selected, unbound]
    assert service.list_for_case(None)[0].revision == unbound
    assert all(
        state.revision.binding is None or state.revision.binding.case_id == "case-b"
        for state in service.list_for_case("case-b")
    )


def test_case_list_prioritizes_active_then_bound_then_unbound_before_paging(desk):
    _, service = desk
    earlier = bind_named(desk, "a-bound", "case-a")
    active = bind_named(desk, "z-active", "case-a")
    other_case = bind_named(desk, "b-other-case", "case-b")
    unbound = service.create_draft(author="Mina", brief_id="a-unbound")
    assert [state.revision for state in service.list_for_case("case-a", limit=2)] == [
        active,
        earlier,
    ]
    assert [state.revision for state in service.list_for_case("case-a", limit=2, offset=2)] == [
        unbound
    ]
    assert [state.revision for state in service.list_for_case("case-b")] == [other_case, unbound]
    assert [state.revision for state in service.list_for_case(None)] == [unbound]


def test_case_list_includes_latest_imported_revision_without_head_promotion(tmp_path, desk):
    _, source = desk
    first = bind_named(desk, "imported-a", "case-a")
    latest = source.save_draft(
        first.brief_id,
        fields(purpose="Latest imported question"),
        base_revision_id=first.revision_id,
        expected_head_revision_id=first.revision_id,
        author="Mina",
    )
    bind_named(desk, "imported-b", "case-b")
    unbound = source.create_draft(author="Mina", brief_id="imported-unbound")
    reviews = ReviewRepository(tmp_path / "case-list-receiver.sqlite3")
    reviews.initialize()
    reviews.import_bundle(bundle())
    reviews.import_bundle(bundle("case-b"))
    repository = PlanningBriefRepository(reviews._connection)
    repository.initialize()
    receiver = PlanningBriefService(repository)
    receiver.import_history(source.history_for_case("case-a"))
    receiver.import_history(source.history_for_case("case-b"))
    receiver.import_history(source.history(unbound.brief_id))
    states = receiver.list_for_case("case-a")
    assert [state.revision for state in states] == [latest, unbound]
    assert all(state.imported and state.head_revision_id is None for state in states)
    assert receiver.state_for_case("case-a") is None
    with reviews._connection() as connection:
        assert (
            connection.execute("SELECT COUNT(*) FROM monitoring_brief_active_cases").fetchone()[0]
            == 0
        )


def test_case_list_preserves_byte_budget_and_never_reads_saved_case_bodies(desk, monkeypatch):
    import evidence_explorer.planning_briefs.persistence as persistence

    _, service = desk
    revision = bind_named(desk, "selected", "case-a")

    def forbidden(*args, **kwargs):
        raise AssertionError("A case picker must not deserialize saved evidence")

    monkeypatch.setattr(service.repository, "_case_binding", forbidden)
    assert service.list_for_case("case-a")[0].revision == revision
    with pytest.raises(ValueError, match="at most 50"):
        service.list_for_case("case-a", limit=51)
    monkeypatch.setattr(persistence, "MAX_HISTORY_BYTES", 1024)
    with pytest.raises(ValueError, match="byte budget"):
        service.list_for_case("case-a")


def shared_records_other_case():
    payload = bundle().model_dump(mode="json")
    payload["case"].update(id="other-case", question="A separate case using supplied records")
    return EvidenceBundle.model_validate(payload)


def foreign_revision(ancestor, other_case, number):
    from evidence_explorer.planning_briefs.models import BriefBinding

    binding = BriefBinding(
        case_id=other_case.case.id,
        source_case_fingerprint=other_case.fingerprint(),
        scope=scope_identity(other_case),
    )
    return BriefRevision(
        brief_id=ancestor.brief_id,
        revision_id=f"foreign-case-revision-{number}",
        number=number,
        parent_revision_id=ancestor.revision_id,
        fields=ancestor.fields,
        binding=binding,
        content_hash=revision_content_hash(ancestor.fields, binding),
        author="External analyst",
        created_at=ancestor.created_at,
    )


def test_imported_unbound_ancestor_cannot_be_adopted_into_another_case(tmp_path, desk):
    _, source = desk
    revision = bound(desk)
    history = source.history_for_case("case-a")
    ancestor = history.revisions[0]
    assert ancestor.binding is None
    other = shared_records_other_case()
    reviews = ReviewRepository(tmp_path / "ancestor-receiver.sqlite3")
    reviews.initialize()
    reviews.import_bundle(bundle())
    reviews.import_bundle(other)
    repository = PlanningBriefRepository(reviews._connection)
    repository.initialize()
    receiver = PlanningBriefService(repository)
    receiver.import_history(history)
    before = receiver.context_token_for_case("case-a")
    with pytest.raises(ValueError, match="Copy its fields to a new brief"):
        receiver.adopt_revision(
            ancestor.revision_id,
            other.case.id,
            expected_head_revision_id=None,
            expected_case_fingerprint=other.fingerprint(),
            author="Receiving analyst",
        )
    assert receiver.load(revision.brief_id).head_revision_id is None
    assert receiver.context_token_for_case("case-a") == before
    assert receiver.history_for_case(other.case.id) == PlanningHistory()
    validate_history_for_bundle(receiver.history_for_case("case-a"), bundle())


def test_unbound_local_head_cannot_bind_elsewhere_after_imported_descendant(desk):
    reviews, service = desk
    ancestor = service.create_draft(author="Mina", fields=fields())
    other = shared_records_other_case()
    reviews.import_bundle(other)
    descendant = foreign_revision(ancestor, other, 2)
    service.import_history(PlanningHistory(revisions=(ancestor, descendant)))
    assert service.load(ancestor.brief_id).revision == ancestor
    with pytest.raises(ValueError, match="Copy its fields to a new brief"):
        service.bind(
            ancestor.brief_id,
            "case-a",
            expected_head_revision_id=ancestor.revision_id,
            expected_case_fingerprint=reviews.get_case_fingerprint("case-a"),
            author="Mina",
        )
    assert service.load(ancestor.brief_id).revision == ancestor
    assert service.state_for_case("case-a") is None
    assert service.history_for_case("case-a") == PlanningHistory()


def test_import_rejects_existing_and_incoming_cross_case_lineage_union(desk):
    reviews, service = desk
    revision = bound(desk)
    original = service.history_for_case("case-a")
    ancestor = original.revisions[0]
    other = shared_records_other_case()
    incoming = PlanningHistory(revisions=(ancestor, foreign_revision(ancestor, other, 3)))
    validate_history_for_bundle(incoming, other)
    token = service.context_token_for_case("case-a")
    with pytest.raises(ValueError, match="another case"):
        service.import_history(incoming)
    assert service.load(revision.brief_id).revision == revision
    assert service.history_for_case("case-a") == original
    assert service.context_token_for_case("case-a") == token
    assert service.history_for_case(other.case.id) == PlanningHistory()
    assert reviews.get_bundle("case-a").fingerprint() == bundle().fingerprint()


def test_cross_case_package_branch_rolls_back_case_before_artifact_install(tmp_path, monkeypatch):
    from evidence_explorer.config import Settings
    from evidence_explorer.review.integrations import workspace
    from evidence_explorer.review.integrations.packages import export_case_package

    settings = Settings(data_dir=tmp_path / "workspace")
    repository = workspace.WorkspaceRepository(settings.db_path)
    repository.initialize()
    repository.import_bundle(bundle())
    application = workspace.WorkspaceService(repository, settings, tmp_path / "artifacts")
    revision = bound((repository, application.planning_briefs))
    original = application.planning_briefs.history_for_case("case-a")
    ancestor = original.revisions[0]
    other = shared_records_other_case()
    incoming = PlanningHistory(revisions=(ancestor, foreign_revision(ancestor, other, 3)))
    package = export_case_package(other, planning_history=incoming)

    def forbidden(*args, **kwargs):
        raise AssertionError("Cross-case lineage must fail before artifact installation")

    monkeypatch.setattr(workspace, "install_case_artifacts", forbidden)
    with pytest.raises(ValueError, match="another case"):
        application.import_package(package)
    assert {row["id"] for row in repository.list_cases()} == {"case-a"}
    assert application.planning_briefs.state_for_case("case-a").revision == revision
    assert application.planning_briefs.history_for_case("case-a") == original
    application.export_package("case-a")


def test_active_pointer_cannot_return_another_cases_head(desk):
    reviews, service = desk
    bind_named(desk, "plan-a", "case-a")
    second = bind_named(desk, "plan-b", "case-b")
    with reviews._connection(write=True) as connection:
        connection.execute(
            "UPDATE monitoring_brief_active_cases SET brief_id=? WHERE case_id='case-a'",
            (second.brief_id,),
        )
    assert service.state_for_case("case-a") is None
    assert service.state_for_case("case-b").revision == second


def test_unbound_head_save_requires_explicit_adoption_after_bound_history_import(desk):
    from evidence_explorer.review.integrations.packages import export_case_package

    reviews, service = desk
    ancestor = service.create_draft(author="Mina", fields=fields())
    other = shared_records_other_case()
    reviews.import_bundle(other)
    descendant = foreign_revision(ancestor, other, 2)
    service.import_history(PlanningHistory(revisions=(ancestor, descendant)))
    original_history = service.history_for_case(other.case.id)
    original_token = service.context_token_for_case(other.case.id)
    original_package = export_case_package(other, planning_history=original_history)

    with pytest.raises(VersionConflictError, match="Adopt a bound revision"):
        service.save_draft(
            ancestor.brief_id,
            fields(purpose="An unbound edit after imported binding"),
            base_revision_id=ancestor.revision_id,
            expected_head_revision_id=ancestor.revision_id,
            author="Mina",
        )

    assert service.load(ancestor.brief_id).revision == ancestor
    assert service.history_for_case(other.case.id) == original_history
    assert service.context_token_for_case(other.case.id) == original_token
    assert service.state_for_case(other.case.id) is None
    assert (
        export_case_package(other, planning_history=service.history_for_case(other.case.id))
        == original_package
    )

    adopted = service.adopt_revision(
        descendant.revision_id,
        other.case.id,
        expected_head_revision_id=ancestor.revision_id,
        expected_case_fingerprint=other.fingerprint(),
        author="Mina",
    )
    edited = service.save_draft(
        ancestor.brief_id,
        fields(purpose="An explicitly adopted scope edit"),
        base_revision_id=adopted.revision_id,
        expected_head_revision_id=adopted.revision_id,
        author="Mina",
    )
    assert edited.binding == adopted.binding
    assert service.load(ancestor.brief_id).revision == edited
    assert service.state_for_case(other.case.id).revision == edited
    assert service.context_token_for_case(other.case.id) != original_token
    validate_history_for_bundle(service.history_for_case(other.case.id), other)
    export_case_package(other, planning_history=service.history_for_case(other.case.id))
