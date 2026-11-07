"""A saved activity signal needs exact local planning, time and declared coverage."""

import hashlib
from copy import deepcopy
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from evidence_explorer.collection.models import CollectedBatch, CollectionSource
from evidence_explorer.collection.persistence import CollectionRepository
from evidence_explorer.collection.scoping import scoped_bundle
from evidence_explorer.planning_briefs.models import (
    BriefBinding,
    BriefConfirmation,
    BriefRevision,
    BriefState,
    DraftFields,
    digest,
    revision_content_hash,
)
from evidence_explorer.planning_briefs.scope import scope_identity
from evidence_explorer.review.contracts import EvidenceBundle
from evidence_explorer.review.integrations.sources import SourceItem
from evidence_explorer.use_cases.contracts import AlertRule, WindowDefinition
from evidence_explorer.use_cases.saved_activity import (
    SavedActivityRequest,
    evaluate_saved_activity,
)


def _rule(**changes):
    return AlertRule.model_validate(
        {
            "id": "fixture:explicit-rule",
            "title": "Explicit fictional saved-record activity comparison",
            "metric": "declared saved record snapshots",
            "threshold_percent": 25,
            "threshold_basis": "analyst_configured",
            "direction": "increase",
            "comparison_basis": "equal_duration_counts",
            "min_baseline_count": 2,
            "min_current_count": 0,
            "min_coverage_fraction": 1,
            **changes,
        }
    )


def _windows():
    return (
        WindowDefinition(id="baseline", start="2026-11-06T10:00:00Z", end="2026-11-06T11:00:00Z"),
        WindowDefinition(
            id="observation", start="2026-11-06T11:00:00Z", end="2026-11-06T12:00:00Z"
        ),
    )


def _bundle(*, windows=None, times=None):
    windows = windows or _windows()
    times = times or [
        "2026-11-06T10:00:00Z",
        "2026-11-06T10:59:59Z",
        "2026-11-06T11:00:00Z",
        "2026-11-06T11:30:00Z",
        "2026-11-06T11:59:59Z",
        "2026-11-06T12:00:00Z",
    ]
    records = []
    for index, stamp in enumerate(times):
        text = f"Harmless fictional saved observation {index}."
        records.append(
            {
                "id": f"record:{index}",
                "kind": "public_post",
                "text": text,
                "content_hash": hashlib.sha256(text.encode()).hexdigest(),
                "source_label": "Explicitly fictional saved sample",
                "published_at": stamp,
                "acquired_at": stamp,
                "metadata": {
                    "synthetic": True,
                    "platform": "telegram",
                    "channel_id": "fictional",
                    "native_id": str(index),
                    "event_time": stamp,
                },
            }
        )
    records.append(
        {
            **records[0],
            "id": "metadata:unrelated",
            "kind": "metadata",
            "published_at": None,
            "acquired_at": None,
            "metadata": {"synthetic": True},
        }
    )
    declaration = {
        "policy": "saved-activity-coverage/v1",
        "timestamp_basis": "publication",
        "record_kind": "public_post",
        "source_refs": [row["id"] for row in records],
    }
    for name, window in zip(("baseline", "observation"), windows, strict=True):
        declaration[name] = {
            "start": window.start.isoformat(),
            "end": window.end.isoformat(),
            "coverage_status": "complete",
            "coverage_fraction": 1,
            "evidence": "Explicit complete fictional bounded sample assertion; no platform claim.",
        }
    return EvidenceBundle.model_validate(
        {
            "case": {
                "id": "fixture:activity",
                "title": "Fictional saved activity",
                "question": "Which declared saved sample needs review?",
                "synthetic": True,
                "coverage": {"saved_activity_coverage": declaration},
            },
            "records": records,
            "fragments": [],
            "runs": [],
            "findings": [],
        }
    )


def _state(bundle, *, rule=None, windows=None):
    rule = rule or _rule()
    baseline, observation = windows or _windows()
    scope = scope_identity(bundle)
    fields = DraftFields(
        use_case_id="public_information_monitoring",
        purpose="Count explicit fictional snapshots.",
        public_subjects_or_topics=("Fictional public topic",),
        source_selection=scope.source_refs,
        reviewer_role="Fictional analyst",
        expected_output="reviewed_brief",
        baseline_window=baseline,
        observation_window=observation,
        alert_rule=rule,
    )
    binding = BriefBinding(
        case_id=bundle.case.id, source_case_fingerprint=bundle.fingerprint(), scope=scope
    )
    revision = BriefRevision(
        brief_id="fixture:brief",
        revision_id="fixture:bound-revision",
        number=2,
        parent_revision_id="fixture:unbound-draft",
        fields=fields,
        binding=binding,
        content_hash=revision_content_hash(fields, binding),
        author="Fictional analyst",
        created_at=datetime(2026, 11, 7, tzinfo=UTC),
    )
    confirmation = BriefConfirmation(
        confirmation_id="fixture:confirmation",
        brief_id=revision.brief_id,
        revision_id=revision.revision_id,
        binding_digest=digest(binding.model_dump(mode="json")),
        reviewer="Fictional analyst",
        confirmed_at=datetime(2026, 11, 7, tzinfo=UTC),
    )
    return BriefState(
        revision=revision,
        confirmation=confirmation,
        status="confirmed",
        head_revision_id=revision.revision_id,
        is_head=True,
        is_active_for_case=True,
        imported=False,
        confirmation_is_local=True,
    )


def _request(*, rule=None, **changes):
    return SavedActivityRequest(
        rule=rule or _rule(),
        timestamp_basis=changes.get("timestamp_basis", "publication"),
        record_kind=changes.get("record_kind", "public_post"),
    )


def _edit(bundle, edit):
    data = bundle.model_dump(mode="json")
    edit(data)
    return EvidenceBundle.model_validate(data)


def _collection(
    bundle, *, status="succeeded", empty_status="succeeded", legacy=False, include_empty=False
):
    data = bundle.model_dump(mode="json")
    observations = [
        {
            "source_id": "source:a",
            "item_key": digest(["telegram", "fictional", record["metadata"]["native_id"]]),
            "payload_hash": digest(record),
            "semantic_hash": digest(record),
            "content_hash": record["content_hash"],
            "first_observed_at": "2026-11-06T12:00:00Z",
            "observed_at": "2026-11-06T12:00:00Z",
            "attribution": "observed",
        }
        for record in data["records"]
        if record["kind"] == "public_post"
    ]
    if legacy:
        observations.append(
            {"source_id": "source:a", "item_key": "legacy:unknown", "attribution": "unknown"}
        )
    sources = [
        {
            "id": "source:a",
            "last_status": status,
            "item_count": len(observations),
            "included_count": len(observations) - int(legacy),
            "unattributed_count": int(legacy),
            "omitted_version_count": 0,
            "version_omissions": [],
            "ready": True,
        },
    ]
    if include_empty:
        sources.append(
            {
                "id": "source:empty",
                "last_status": empty_status,
                "item_count": 0,
                "included_count": 0,
                "unattributed_count": 0,
                "omitted_version_count": 0,
                "version_omissions": [],
                "ready": None,
            },
        )
    scope = {
        "version_policy": "latest_selected_observation_then_source_id",
        "source_ids": [row["id"] for row in sources],
        "sources": sources,
        "total_items": len(observations),
        "record_count": len(observations),
        "truncated": False,
        "observation_manifest": observations,
        "observations_fingerprint": digest(observations),
    }
    scope["fingerprint"] = digest(scope)
    data["collection_scope"] = scope
    data["case"]["coverage"]["collection_scope"] = deepcopy(scope)
    data["case"]["coverage"]["saved_activity_coverage"]["source_refs"] = scope["source_ids"]
    return EvidenceBundle.model_validate(data)


def test_exact_saved_ids_half_open_boundaries_and_read_only_result_identity():
    bundle = _bundle()
    state = _state(bundle)
    before_bundle, before_state = bundle.model_dump_json(), state.model_dump_json()
    result = evaluate_saved_activity(bundle, state, _request())
    assert result.status == "review_needed"
    assert result.count_unit == "saved_record_snapshots"
    assert result.baseline_record_ids == ("record:0", "record:1")
    assert result.observation_record_ids == ("record:2", "record:3", "record:4")
    assert result.outside_window_record_ids == ("record:5",)
    assert result.omitted_record_ids == ("metadata:unrelated",)
    assert result.baseline_count == 2 and result.observation_count == 3
    assert result.evaluation.relative_change_percent == 50
    assert result.evaluation.origin == "synthetic"
    assert set(result.evaluation.record_ids) == set(
        result.baseline_record_ids + result.observation_record_ids
    )
    assert result.scope_basis == "saved_evidence_manifest" and not result.source_gaps
    assert result.baseline.coverage_fraction == result.observation.coverage_fraction == 1
    assert result.coverage.baseline.evidence
    for fingerprint in (
        result.case_fingerprint,
        result.evidence_fingerprint,
        result.scope_fingerprint,
        result.request_fingerprint,
        result.revision_fingerprint,
        result.binding_fingerprint,
        result.context_fingerprint,
    ):
        assert len(fingerprint) == 64
    assert result == evaluate_saved_activity(bundle, state, _request())
    assert bundle.model_dump_json() == before_bundle and state.model_dump_json() == before_state


@pytest.mark.parametrize("bad_time", [None, "not-a-time", "2026-11-06T10:30:00"])
def test_candidate_unknown_or_naive_time_never_falls_back_to_other_metadata(bad_time):
    bundle = _edit(_bundle(), lambda data: data["records"][0].update(published_at=bad_time))
    result = evaluate_saved_activity(bundle, _state(bundle), _request())
    assert result.status == "unavailable"
    assert result.unknown_time_record_ids == ("record:0",)
    assert result.baseline_count is result.observation_count is None
    assert result.evaluation is None


@pytest.mark.parametrize("basis", ["acquisition", "event"])
def test_only_requested_top_level_or_explicit_event_field_is_used(basis):
    bundle = _bundle()
    field = "acquired_at" if basis == "acquisition" else "event_time"
    data = bundle.model_dump(mode="json")
    data["case"]["coverage"]["saved_activity_coverage"]["timestamp_basis"] = basis
    target = data["records"][0] if basis == "acquisition" else data["records"][0]["metadata"]
    target[field] = None
    data["records"][0]["metadata"].update(
        observed_at="2026-11-06T10:00:00Z", first_posted_exercise_week=1
    )
    bundle = EvidenceBundle.model_validate(data)
    result = evaluate_saved_activity(bundle, _state(bundle), _request(timestamp_basis=basis))
    assert result.status == "unavailable" and result.unknown_time_record_ids == ("record:0",)
    if basis == "event":
        assert any("source-declared event assertion" in line for line in result.limitations)


@pytest.mark.parametrize(
    "change",
    ["missing", "partial", "unknown", "boolean", "window", "kind", "time", "sources", "policy"],
)
def test_only_exact_complete_typed_saved_coverage_can_evaluate(change):
    data = _bundle().model_dump(mode="json")
    coverage = data["case"]["coverage"]["saved_activity_coverage"]
    if change == "missing":
        del data["case"]["coverage"]["saved_activity_coverage"]
    elif change == "partial":
        coverage["baseline"].update(coverage_status="partial", coverage_fraction=0.5)
    elif change == "unknown":
        coverage["observation"].update(coverage_status="unknown", coverage_fraction=None)
    elif change == "boolean":
        coverage["baseline"]["coverage_fraction"] = True
    elif change == "window":
        coverage["baseline"]["end"] = "2026-11-06T10:30:00Z"
    elif change == "kind":
        coverage["record_kind"] = "article"
    elif change == "time":
        coverage["timestamp_basis"] = "event"
    elif change == "sources":
        coverage["source_refs"] = ["not-the-saved-scope"]
    else:
        coverage["policy"] = "unknown-policy"
    bundle = EvidenceBundle.model_validate(data)
    result = evaluate_saved_activity(bundle, _state(bundle), _request())
    assert result.status == "unavailable" and result.evaluation is None
    if change in {"partial", "unknown"}:
        assert result.coverage is not None


@pytest.mark.parametrize(
    "change", ["evidence", "coverage", "cross-case", "unconfirmed", "imported", "head", "rule"]
)
def test_stale_cross_case_imported_or_unconfirmed_plan_cannot_evaluate(change):
    bundle = _bundle()
    state = _state(bundle)
    request = _request()
    if change == "evidence":
        bundle = _edit(bundle, lambda data: data["records"][0].update(source_label="Changed"))
    elif change == "coverage":
        bundle = _edit(
            bundle,
            lambda data: data["case"]["coverage"]["saved_activity_coverage"]["baseline"].update(
                evidence="A changed immutable saved assertion"
            ),
        )
    elif change == "cross-case":
        bundle = _edit(bundle, lambda data: data["case"].update(id="other-case"))
    elif change == "unconfirmed":
        state = state.model_copy(update={"confirmation_is_local": False})
    elif change == "imported":
        state = state.model_copy(update={"imported": True})
    elif change == "head":
        state = state.model_copy(update={"is_head": False})
    else:
        request = _request(rule=_rule(threshold_percent=10))
    result = evaluate_saved_activity(bundle, state, request)
    assert result.status == "unavailable" and result.evaluation is None


def test_missing_state_and_unknown_confirmed_windows_are_unavailable():
    bundle = _bundle()
    assert evaluate_saved_activity(bundle, None, _request()).status == "unavailable"
    unknown = WindowDefinition(id="unknown")
    state = _state(bundle, windows=(unknown, unknown))
    assert evaluate_saved_activity(bundle, state, _request()).status == "unavailable"


@pytest.mark.parametrize("empty_status", ["failed", "unknown", "succeeded"])
def test_failed_or_unknown_empty_selected_sources_are_not_fabricated(empty_status):
    bundle = _collection(_bundle(), empty_status=empty_status, include_empty=True)
    result = evaluate_saved_activity(bundle, _state(bundle), _request())
    assert result.status == "unavailable"
    assert any("source:empty" in gap for gap in result.source_gaps)
    assert "source:empty" in result.source_refs
    assert all("source:empty" not in rid for rid in result.baseline_record_ids)


def test_exact_collection_scope_is_preserved_without_fabricated_source_references():
    bundle = _collection(_bundle())
    result = evaluate_saved_activity(bundle, _state(bundle), _request())
    assert result.status == "review_needed" and result.scope_basis == "collection_scope"
    assert result.source_refs == ("source:a",)
    assert not result.source_gaps
    assert result.baseline.source_ids == result.source_refs
    assert result.baseline_count + result.observation_count == 5


def test_item_limit_and_differing_version_omissions_have_distinct_honest_counts():
    data = _collection(_bundle()).model_dump(mode="json")
    scope = data["collection_scope"]
    scope.update(total_items=10, truncated=True)
    scope["sources"][0]["omitted_version_count"] = 2
    data["case"]["coverage"]["collection_scope"] = deepcopy(scope)
    bundle = EvidenceBundle.model_validate(data)
    result = evaluate_saved_activity(bundle, _state(bundle), _request())
    assert result.status == "unavailable"
    assert result.item_limit_omitted_record_count == 4
    assert result.unpackaged_source_observation_count == 2
    assert any("item limit" in gap for gap in result.source_gaps)
    assert all(not rid.startswith("invented") for rid in result.baseline_record_ids)


@pytest.mark.parametrize("readiness", [None, False])
def test_frozen_connection_readiness_is_disclosed_without_claiming_historical_coverage(readiness):
    data = _collection(_bundle()).model_dump(mode="json")
    data["collection_scope"]["sources"][0]["ready"] = readiness
    data["case"]["coverage"]["collection_scope"] = deepcopy(data["collection_scope"])
    bundle = EvidenceBundle.model_validate(data)
    result = evaluate_saved_activity(bundle, _state(bundle), _request())
    assert result.status == "review_needed"
    assert result.source_disclosures
    assert "does not establish historical window coverage" in result.source_disclosures[0]
    assert result.source_disclosures[0] in result.limitations
    assert result.coverage.baseline.evidence


def test_explicit_snapshot_count_unit_is_accepted_but_another_unit_is_not():
    data = _bundle().model_dump(mode="json")
    coverage = data["case"]["coverage"]["saved_activity_coverage"]
    coverage["count_unit"] = "saved_record_snapshots"
    bundle = EvidenceBundle.model_validate(data)
    assert evaluate_saved_activity(bundle, _state(bundle), _request()).status == "review_needed"
    coverage["count_unit"] = "independent_native_posts"
    bundle = EvidenceBundle.model_validate(data)
    assert evaluate_saved_activity(bundle, _state(bundle), _request()).status == "unavailable"


def test_review_history_changes_do_not_stale_saved_activity_evidence_identity():
    data = _bundle().model_dump(mode="json")
    data["runs"] = [
        {
            "id": "stored:run",
            "input_fingerprint": "stored:snapshot",
            "input_manifest": [row["id"] for row in data["records"]],
            "method": "Fictional stored method",
            "method_version": "1",
            "created_at": "2026-11-06T12:00:00Z",
        }
    ]
    data["findings"] = [
        {
            "snapshot_id": "stored:finding",
            "lineage_id": "stored:finding",
            "version": 1,
            "run_id": "stored:run",
            "kind": "observation",
            "statement": "A fictional stored observation requiring separate review.",
        }
    ]
    bundle = EvidenceBundle.model_validate(data)
    state = _state(bundle)
    before = evaluate_saved_activity(bundle, state, _request())
    data["scenarios"] = [{"id": "review:scenario", "base_run_id": "stored:run"}]
    data["reviews"] = [
        {
            "id": "review:decision",
            "finding_snapshot_id": "stored:finding",
            "run_id": "stored:run",
            "scenario_id": "review:scenario",
            "reviewer": "Fictional analyst",
            "disposition": "withhold",
            "reason": "Fictional recorded finding decision.",
            "timestamp": "2026-11-07T00:00:00Z",
        }
    ]
    reopened = EvidenceBundle.model_validate(data)
    after = evaluate_saved_activity(reopened, state, _request())
    assert after.case_fingerprint != before.case_fingerprint
    assert after.context_fingerprint == before.context_fingerprint
    assert after.status == "review_needed"


@pytest.mark.parametrize("change", ["legacy", "unattributed", "foreign-native"])
def test_collection_candidates_need_exact_observed_selected_source_content(change):
    bundle = _collection(_bundle(), legacy=change == "legacy")
    if change == "unattributed":
        bundle = _edit(bundle, lambda data: data["records"][0].update(metadata={"synthetic": True}))
    elif change == "foreign-native":
        bundle = _edit(
            bundle, lambda data: data["records"][0]["metadata"].update(native_id="unobserved")
        )
    result = evaluate_saved_activity(bundle, _state(bundle), _request())
    assert result.status == "unavailable" and result.source_gaps


def test_distinct_saved_snapshots_count_separately_without_native_or_content_dedup_claim():
    bundle = _bundle()
    data = bundle.model_dump(mode="json")
    data["records"][1].update(
        text=data["records"][0]["text"],
        content_hash=data["records"][0]["content_hash"],
        metadata=deepcopy(data["records"][0]["metadata"]),
    )
    bundle = EvidenceBundle.model_validate(data)
    result = evaluate_saved_activity(bundle, _state(bundle), _request())
    assert result.baseline_count == 2 and result.count_unit == "saved_record_snapshots"
    assert any("independent corroboration" in line for line in result.limitations)
    invalid = bundle.model_copy(update={"records": [*bundle.records, bundle.records[0]]})
    assert evaluate_saved_activity(invalid, _state(bundle), _request()).status == "unavailable"


def test_dst_fold_uses_elapsed_utc_boundaries_and_never_double_counts_shared_edge():
    zone = ZoneInfo("Europe/Berlin")
    windows = (
        WindowDefinition(
            id="baseline",
            start=datetime(2026, 10, 25, 1, tzinfo=zone),
            end=datetime(2026, 10, 25, 2, tzinfo=zone, fold=0),
        ),
        WindowDefinition(
            id="observation",
            start=datetime(2026, 10, 25, 2, tzinfo=zone, fold=0),
            end=datetime(2026, 10, 25, 2, tzinfo=zone, fold=1),
        ),
    )
    times = [
        "2026-10-25T01:00:00+02:00",
        "2026-10-25T01:59:59+02:00",
        "2026-10-25T02:00:00+02:00",
        "2026-10-25T02:30:00+02:00",
        "2026-10-25T02:59:59+02:00",
        "2026-10-25T02:00:00+01:00",
    ]
    bundle = _bundle(windows=windows, times=times)
    result = evaluate_saved_activity(bundle, _state(bundle, windows=windows), _request())
    assert result.status == "review_needed"
    assert result.baseline_count == 2 and result.observation_count == 3
    assert result.outside_window_record_ids == ("record:5",)
    assert not set(result.baseline_record_ids) & set(result.observation_record_ids)


def test_explicit_exposure_rates_and_missing_denominator_use_existing_evaluator():
    rule = _rule(comparison_basis="exposure_normalized")
    data = _bundle().model_dump(mode="json")
    coverage = data["case"]["coverage"]["saved_activity_coverage"]
    coverage["baseline"].update(exposure=2, exposure_unit="declared sample opportunities")
    coverage["observation"].update(exposure=6, exposure_unit="declared sample opportunities")
    bundle = EvidenceBundle.model_validate(data)
    result = evaluate_saved_activity(bundle, _state(bundle, rule=rule), _request(rule=rule))
    assert result.status == "not_triggered"
    assert result.evaluation.baseline_value == 1
    assert result.evaluation.observation_value == 0.5
    assert result.evaluation.relative_change_percent == -50
    del coverage["observation"]["exposure"]
    bundle = EvidenceBundle.model_validate(data)
    result = evaluate_saved_activity(bundle, _state(bundle, rule=rule), _request(rule=rule))
    assert result.status == "unavailable" and "exposure" in result.evaluation.summary


def test_zero_and_insufficient_baseline_do_not_make_percentage_signals():
    for times in (
        ["2026-11-06T11:05:00Z"] * 6,
        ["2026-11-06T10:05:00Z", *(["2026-11-06T11:05:00Z"] * 5)],
    ):
        bundle = _bundle(times=times)
        result = evaluate_saved_activity(bundle, _state(bundle), _request())
        assert result.status == "unavailable"
        assert result.evaluation.relative_change_percent is None
        assert "baseline" in result.evaluation.summary


@pytest.mark.parametrize("record_id", ["record:0", "record:5"])
def test_declared_included_native_record_missing_is_unavailable_even_after_reconfirmation(
    record_id,
):
    rule = _rule(min_baseline_count=1, threshold_percent=100)
    full = _collection(_bundle())
    before = evaluate_saved_activity(full, _state(full, rule=rule), _request(rule=rule))
    assert before.status == "not_triggered"
    assert before.evaluation.relative_change_percent == 50
    missing = _edit(
        full,
        lambda data: data.update(
            records=[row for row in data["records"] if row["id"] != record_id]
        ),
    )
    result = evaluate_saved_activity(missing, _state(missing, rule=rule), _request(rule=rule))
    assert result.status == "unavailable" and result.evaluation is None
    assert any("included native record is missing" in gap for gap in result.source_gaps)


def test_inverse_reconciliation_includes_native_records_of_other_kinds_and_rejects_repeats():
    full = _edit(
        _collection(_bundle()), lambda data: data["records"][0].update(kind="different_kind")
    )
    missing = _edit(full, lambda data: data.update(records=data["records"][1:]))
    assert evaluate_saved_activity(missing, _state(missing), _request()).source_gaps
    repeated = _edit(
        full,
        lambda data: data["records"].append({**data["records"][1], "id": "extra:same-native"}),
    )
    result = evaluate_saved_activity(repeated, _state(repeated), _request())
    assert result.status == "unavailable"
    assert any("missing or repeated" in gap for gap in result.source_gaps)


def _producer_collection(tmp_path, *, version="shared", tie=False, repeated_text=False):
    """Use actual collection persistence and the source adapter, without source requests."""
    repository = CollectionRepository(tmp_path / "collection.sqlite3")
    repository.initialize()
    clock = datetime(2026, 11, 7, tzinfo=UTC)
    sources = [
        CollectionSource(
            id=f"source:{key}",
            platform="news",
            label=f"Fictional feed {key}",
            locator=f"https://publisher.example/{key}/feed.xml",
            enabled=False,
            item_limit=20,
        )
        for key in ("a", "b")
    ]
    for source in sources:
        repository.add(source, now=clock)
    original = [
        SourceItem(
            platform="news",
            channel_id="fictional",
            channel_name="Fictional publisher",
            native_id=record.metadata["native_id"],
            text=record.text,
            source_url=f"https://publisher.example/posts/{record.metadata['native_id']}",
            acquired_at=clock.isoformat(),
            published_at=record.published_at,
            provenance={
                "method": "rss_content",
                "content_scope": "feed_content",
                "feed_url": sources[0].locator,
                "retrieved_url": sources[0].locator,
            },
        )
        for record in _bundle().records
        if record.kind == "public_post"
    ]
    if repeated_text:
        original[1] = original[1].model_copy(update={"text": original[0].text})
    for index, source in enumerate(sources):
        items = original
        if index == 1 and version != "shared":
            payload = original[0].model_dump(mode="json")
            if version == "text":
                payload["text"] = "A different fictional source-observed version."
            else:
                payload["published_at"] = "2026-11-06T10:30:00Z"
            items = [SourceItem.model_validate(payload)]
        stamp = clock + timedelta(hours=0 if tie else index)
        job_id = repository.enqueue(source.id, now=stamp)
        job = repository.claim(now=stamp)
        assert job["id"] == job_id
        repository.complete(job, CollectedBatch(tuple(items)), now=stamp)
    snapshot = repository.scope_snapshot([source.id for source in sources])
    bundle = scoped_bundle(snapshot)
    declaration = _bundle().case.coverage["saved_activity_coverage"]
    declaration = {
        **declaration,
        "record_kind": "saved_source_item",
        "source_refs": snapshot["source_ids"],
    }
    bundle = _edit(
        bundle, lambda data: data["case"]["coverage"].update(saved_activity_coverage=declaration)
    )
    return bundle


def test_actual_selected_sources_share_one_native_record_without_false_missing_records(tmp_path):
    bundle = _producer_collection(tmp_path, repeated_text=True)
    request = _request(record_kind="saved_source_item")
    result = evaluate_saved_activity(bundle, _state(bundle), request)
    scope = bundle.model_dump(mode="json")["collection_scope"]
    assert scope["record_count"] == 6
    assert sum(row["included_count"] for row in scope["sources"]) == 12
    assert len(scope["observation_manifest"]) == 12
    assert result.status == "review_needed" and result.source_gaps == ()
    assert result.baseline_count == 2 and result.observation_count == 3
    assert bundle.records[0].id != bundle.records[1].id


@pytest.mark.parametrize("all_records", [False, True])
def test_relabeling_collected_record_kinds_cannot_change_a_reconfirmed_count(all_records, tmp_path):
    bundle = _producer_collection(tmp_path)
    rule = _rule(min_baseline_count=1, threshold_percent=100)
    request = _request(rule=rule, record_kind="saved_source_item")
    before = evaluate_saved_activity(bundle, _state(bundle, rule=rule), request)
    assert before.status == "not_triggered" and before.evaluation.relative_change_percent == 50
    data = bundle.model_dump(mode="json")
    for record in data["records"]:
        if all_records or record["metadata"]["native_id"] == "0":
            record["kind"] = "relabelled_native_snapshot"
    if all_records:
        data["case"]["coverage"]["saved_activity_coverage"]["record_kind"] = (
            "relabelled_native_snapshot"
        )
        request = _request(rule=rule, record_kind="relabelled_native_snapshot")
    relabelled = EvidenceBundle.model_validate(data)
    result = evaluate_saved_activity(relabelled, _state(relabelled, rule=rule), request)
    assert result.status == "unavailable" and result.evaluation is None
    assert any("record kind differs" in gap for gap in result.source_gaps)


@pytest.mark.parametrize("version,tie", [("text", False), ("metadata", False), ("text", True)])
def test_actual_differing_versions_resolve_through_explicit_omissions(tmp_path, version, tie):
    bundle = _producer_collection(tmp_path, version=version, tie=tie)
    request = _request(record_kind="saved_source_item")
    result = evaluate_saved_activity(bundle, _state(bundle), request)
    assert result.status == "review_needed" and result.source_gaps == ()
    assert result.unpackaged_source_observation_count == 1
    assert result.baseline_count == 2 and result.observation_count == 3


@pytest.mark.parametrize("damage", ["omission", "winner", "same-text-metadata", "policy"])
def test_version_omission_never_excuses_missing_or_wrong_chosen_payload(tmp_path, damage):
    data = _producer_collection(tmp_path, version="metadata").model_dump(mode="json")
    scope = data["collection_scope"]
    if damage == "omission":
        scope["sources"][0]["version_omissions"][0]["included_in_case"] = False
    elif damage == "policy":
        del scope["version_policy"]
    else:
        winner = next(row for row in data["records"] if row["metadata"]["native_id"] == "0")
        if damage == "same-text-metadata":
            # The body hash still matches. Activity must not use the wrong source-observed time.
            winner["published_at"] = "2026-11-06T10:00:00Z"
        else:
            data["records"].remove(winner)
            fragments = {row["id"] for row in data["fragments"] if row["record_id"] == winner["id"]}
            data["fragments"] = [row for row in data["fragments"] if row["id"] not in fragments]
            data["links"] = [
                row
                for row in data["links"]
                if row["source"]["id"] != winner["id"]
                and row["target"]["id"] != winner["id"]
                and not set(row["fragment_ids"]) & fragments
            ]
            for run in data["runs"]:
                run["input_manifest"] = [
                    rid for rid in run["input_manifest"] if rid != winner["id"]
                ]
    data["case"]["coverage"]["collection_scope"] = deepcopy(scope)
    bundle = EvidenceBundle.model_validate(data)
    result = evaluate_saved_activity(
        bundle, _state(bundle), _request(record_kind="saved_source_item")
    )
    assert result.status == "unavailable" and result.source_gaps


def test_unversioned_unambiguous_collection_scope_remains_usable():
    data = _collection(_bundle()).model_dump(mode="json")
    del data["collection_scope"]["version_policy"]
    data["case"]["coverage"]["collection_scope"] = deepcopy(data["collection_scope"])
    bundle = EvidenceBundle.model_validate(data)
    result = evaluate_saved_activity(bundle, _state(bundle), _request())
    assert result.status == "review_needed" and result.source_gaps == ()
