"""Saved dashboard counts retain exact identities, gaps and annotation provenance."""

import hashlib

import pytest
from pydantic import ValidationError

from evidence_explorer.review.contracts import EvidenceBundle
from evidence_explorer.social_dashboard import DashboardFilters, project_dashboard
from evidence_explorer.source_bundles import build_source_bundle

NOW = "2026-11-07T12:00:00Z"


def record(
    rid,
    *,
    published=NOW,
    text="A saved account discusses the harbor project.",
    **metadata,
):
    return {
        "id": rid,
        "kind": "saved_source_item",
        "source_label": "Supplied display label",
        "text": text,
        "content_hash": hashlib.sha256(text.encode()).hexdigest(),
        "published_at": published,
        "acquired_at": NOW,
        "metadata": metadata,
    }


def bundle(*records):
    return EvidenceBundle.model_validate(
        {
            "case": {
                "id": "dashboard-case",
                "title": "Saved evidence",
                "question": "What is saved?",
            },
            "records": list(records),
            "fragments": [
                {
                    "id": row["id"] + ":fragment",
                    "record_id": row["id"],
                    "selector": {"kind": "text", "start": 0, "end": len(row["text"])},
                    "quote": row["text"],
                }
                for row in records
            ],
            "runs": [
                {
                    "id": "saved",
                    "input_fingerprint": "saved-fingerprint",
                    "input_manifest": [row["id"] for row in records],
                    "method": "saved_source_import",
                    "method_version": "1",
                    "created_at": NOW,
                }
            ],
            "findings": [],
        }
    )


def annotated(saved, *, run_id="sentiment-1", label="positive", target="harbor project", **changes):
    payload = saved.model_dump(mode="json")
    if not any(run["id"] == run_id for run in payload["runs"]):
        payload["runs"].append(
            {
                "id": run_id,
                "input_fingerprint": "annotation-input",
                "input_manifest": [row["id"] for row in payload["records"]],
                "method": "saved_sentiment_annotation",
                "method_version": "1",
                "created_at": NOW,
            }
        )
    first = payload["records"][0]
    payload["findings"].append(
        {
            "snapshot_id": f"annotation-{len(payload['findings'])}",
            "lineage_id": "annotation",
            "version": 1,
            "run_id": run_id,
            "kind": "inference",
            "statement": "Saved annotation, awaiting review.",
            "details": {
                "domain": "social_sentiment",
                "annotation": {
                    "record_id": first["id"],
                    "content_hash": first["content_hash"],
                    "target": target,
                    "label": label,
                    "model": "saved-local-model",
                    "prompt_version": "sentiment/v1",
                    "fragment_ids": [first["id"] + ":fragment"],
                    **changes,
                },
            },
        }
    )
    return EvidenceBundle.model_validate(payload)


def test_full_saved_counts_are_not_browser_limit_or_preview_limited():
    saved = bundle(
        *(record(f"r-{index:04}", platform="x", channel_id="one") for index in range(251)),
        record("late", text="a" * 300 + " NEEDLE", platform="reddit", channel_id="two"),
    )
    overview = project_dashboard(saved)
    assert overview.coverage.total_records == 252
    assert overview.coverage.matching_records == 252
    assert sum(row.count for row in overview.platforms) == 252
    assert sum(row.count for row in overview.time_buckets) == 252
    filtered = project_dashboard(saved, filters={"text_query": "needle"})
    assert [row.id for row in filtered.rows] == ["late"]
    assert len(filtered.rows[0].preview) == 240
    assert filtered.options.platforms == overview.options.platforms


@pytest.mark.parametrize(
    ("bucket", "expected"),
    [
        ("day", [("2026-10-31", 1), ("2026-11-01", 1), ("2026-11-02", 1)]),
        ("week", [("2026-10-26", 2), ("2026-11-02", 1)]),
        ("month", [("2026-10-01", 1), ("2026-11-01", 2)]),
    ],
)
def test_buckets_use_utc_dates_monday_weeks_and_calendar_months(bucket, expected):
    saved = bundle(
        record("offset", published="2026-11-01T00:30:00+02:00"),
        record("sunday", published="2026-11-01T12:00:00Z"),
        record("monday", published="2026-11-02T00:00:00Z"),
    )
    projected = project_dashboard(saved, filters={"bucket": bucket})
    assert [(item.start, item.count) for item in projected.time_buckets] == expected
    assert {rid for item in projected.time_buckets for rid in item.record_ids} == {
        "offset",
        "sunday",
        "monday",
    }
    assert all(item.end > item.start for item in projected.time_buckets)


def test_unknown_times_never_fall_back_and_dates_are_inclusive_with_separate_unknowns():
    saved = bundle(
        record("missing", published=None),
        record("date", published="2026-11-07"),
        record("naive", published="2026-11-07T12:00:00"),
        record("invalid", published="not-a-date"),
        record("inside"),
        record("outside", published="2026-11-08T00:00:00Z"),
    )
    result = project_dashboard(saved, filters={"start": "2026-11-07", "end": "2026-11-07"})
    assert result.coverage.unknown_timestamp_records == 4
    assert result.coverage.outside_date_records == 1
    assert result.coverage.matching_records == 5
    assert result.time_buckets[0].record_ids == ("inside",)
    assert {row.id: row.time_status for row in result.rows} == {
        "date": "date_only",
        "inside": "known",
        "invalid": "invalid",
        "missing": "missing",
        "naive": "naive",
    }
    acquired = project_dashboard(saved, filters={"time_basis": "acquisition"})
    assert acquired.time_buckets[0].count == 6
    assert acquired.coverage.unknown_timestamp_records == 0


def test_identity_is_namespaced_and_never_inferred_from_display_names_or_article_kind():
    records = [
        record(
            "x-a",
            platform="x",
            channel_id="one",
            author_id="alice",
            channel_name="Same",
        ),
        record(
            "x-b",
            platform="x",
            channel_id="two",
            author_id="alice",
            channel_name="Same",
        ),
        record(
            "reddit",
            platform="reddit",
            channel_id="one",
            author_id="alice",
            channel_name="Same",
        ),
        record("label-only", platform="x", channel_name="Same", author_id="alice"),
        record("no-platform", channel_id="one", author_id="alice"),
        record("article", publisher="Publisher", source_platform="mastodon"),
    ]
    records[-1]["kind"] = "article"
    result = project_dashboard(bundle(*records))
    rows = {row.id: row for row in result.rows}
    assert len({rows[key].channel_key for key in ("x-a", "x-b", "reddit")}) == 3
    assert len({rows[key].author_key for key in ("x-a", "x-b", "reddit")}) == 3
    assert len({rows[key].channel_label for key in ("x-a", "x-b", "reddit")}) == 3
    assert len({rows[key].author_label for key in ("x-a", "x-b", "reddit")}) == 3
    assert rows["x-a"].channel_label == "X · Same (one)"
    assert rows["x-a"].author_label == "X · Same (one) · alice"
    assert rows["label-only"].channel_key is None
    assert rows["label-only"].author_key is None
    assert rows["no-platform"].platform == "unknown"
    assert rows["no-platform"].channel_key is None
    assert rows["article"].platform == "unknown"
    assert result.coverage.unknown_author_records == 3
    one = project_dashboard(bundle(*records), filters={"author_key": rows["x-b"].author_key})
    assert [row.id for row in one.rows] == ["x-b"]


@pytest.mark.parametrize(
    ("flag", "expected"),
    [(True, True), (False, False), (None, None), (1, None), ("false", None)],
)
def test_record_origin_is_literal_not_parent_case_or_truthiness(flag, expected):
    saved = bundle(record("record", synthetic=flag))
    saved.case.synthetic = True
    assert project_dashboard(saved).rows[0].synthetic is expected


def test_exclusion_and_literal_filter_are_read_only_and_keep_denominators_separate():
    saved = bundle(
        record("a", text="Literal [topic] text", platform="x", channel_id="a"),
        record("b"),
    )
    before = saved.model_dump_json()
    result = project_dashboard(
        saved,
        excluded_record_ids=("b", "not-present"),
        filters={"text_query": "[TOPIC]"},
    )
    assert result.coverage.total_records == 2
    assert result.coverage.excluded_records == 1
    assert result.coverage.eligible_records == result.coverage.matching_records == 1
    assert result.rows[0].id == "a"
    assert saved.model_dump_json() == before


def test_sentiment_requires_explicit_run_and_preserves_provenance_and_unclear():
    saved = annotated(bundle(record("one")), label="unclear")
    assert project_dashboard(saved).rows[0].sentiment == "unknown"
    result = project_dashboard(saved, filters={"sentiment_run_id": "sentiment-1"})
    row = result.rows[0]
    assert row.sentiment == "unclear"
    assert row.sentiment_model == "saved-local-model"
    assert row.sentiment_prompt_version == "sentiment/v1"
    assert row.sentiment_run_id == "sentiment-1"
    assert row.sentiment_fragment_ids == ("one:fragment",)
    assert result.sentiment_counts.unclear == 1
    assert result.coverage.unknown_sentiment_records == 0
    assert result.sentiment_targets[0].target == "harbor project"


@pytest.mark.parametrize(
    "change",
    [
        {"content_hash": "wrong"},
        {"fragment_ids": ["missing"]},
        {"fragment_ids": ["two:fragment"]},
        {"fragment_ids": []},
        {"fragment_ids": ["one:fragment", "one:fragment"]},
        {"fragment_ids": [[]]},
        {"label": []},
        {"label": {"positive": True}},
        {"label": True},
        {"target": ""},
        {"model": ""},
        {"prompt_version": None},
    ],
)
def test_invalid_annotation_stays_unknown_without_crashing(change):
    saved = annotated(bundle(record("one"), record("two")), **change)
    result = project_dashboard(saved, filters={"sentiment_run_id": "sentiment-1"})
    assert all(row.sentiment == "unknown" for row in result.rows)
    assert result.coverage.invalid_annotation_records == 1


def test_sentiment_does_not_mix_runs_accept_conflicts_or_failed_runs():
    saved = annotated(bundle(record("one")), label="positive")
    saved = annotated(saved, run_id="sentiment-2", label="negative")
    result = project_dashboard(saved, filters={"sentiment_run_id": "sentiment-1"})
    assert result.rows[0].sentiment == "positive"
    conflicted = annotated(saved, label="negative")
    assert (
        project_dashboard(conflicted, filters={"sentiment_run_id": "sentiment-1"}).rows[0].sentiment
        == "unknown"
    )
    saved.runs[-1].status = "failed"
    failed = project_dashboard(saved, filters={"sentiment_run_id": "sentiment-2"})
    assert failed.rows[0].sentiment == "unknown"
    assert failed.coverage.sentiment_run_available is False
    assert "sentiment-2" not in {option.value for option in failed.options.sentiment_runs}


def test_multiple_targets_are_separate_and_disable_combined_sentiment_trend():
    saved = annotated(bundle(record("one"), record("two")), target="policy")
    second = saved.records[1]
    saved = annotated(
        saved,
        target="speaker",
        label="negative",
        record_id=second.id,
        content_hash=second.content_hash,
        fragment_ids=["two:fragment"],
    )
    result = project_dashboard(saved, filters={"sentiment_run_id": "sentiment-1"})
    assert result.sentiment_comparable is False
    assert [(item.target, item.record_ids) for item in result.sentiment_targets] == [
        ("policy", ("one",)),
        ("speaker", ("two",)),
    ]


def test_native_repost_has_existing_forward_direction_and_no_duplicate_metadata_edge():
    saved = build_source_bundle(
        {
            "source_schema": "1.0",
            "case": {
                "title": "Saved repost",
                "question": "What is declared?",
                "synthetic": False,
            },
            "items": [
                {
                    "platform": "x",
                    "channel_id": "one",
                    "channel_name": "One",
                    "native_id": "1",
                    "text": "Original saved text",
                    "acquired_at": NOW,
                },
                {
                    "platform": "x",
                    "channel_id": "two",
                    "channel_name": "Two",
                    "native_id": "2",
                    "text": "Reposted saved text",
                    "acquired_at": NOW,
                    "repost_of": {
                        "platform": "x",
                        "channel_id": "one",
                        "native_id": "1",
                    },
                },
            ],
        }
    )
    result = project_dashboard(saved)
    assert result.coverage.relationship_total == 1
    edge = result.edges[0]
    by_native = {row.metadata["native_id"]: row.id for row in saved.records}
    assert (edge.source_record_id, edge.target_record_id) == (
        by_native["1"],
        by_native["2"],
    )
    assert edge.link_ids


def test_shared_url_graph_is_bounded_without_inventing_same_content_links():
    saved = bundle(
        *(
            record(f"r-{index:03}", shared_urls=["https://example.org/saved"])
            for index in range(205)
        )
    )
    result = project_dashboard(saved)
    assert result.coverage.relationship_total == 204
    assert result.coverage.relationship_shown == len(result.edges) == 200
    assert {edge.relation for edge in result.edges} == {"shared_url"}
    assert result.coverage.matching_records == 205
    excluded = project_dashboard(saved, excluded_record_ids=("r-000",))
    assert all(
        "r-000" not in (edge.source_record_id, edge.target_record_id) for edge in excluded.edges
    )


@pytest.mark.parametrize(
    "filters",
    [
        {"bucket": "hour"},
        {"start": "2026-11-08", "end": "2026-11-07"},
        {"start": "20261107"},
        {"text_query": 1},
        {"unexpected": True},
    ],
)
def test_filters_are_strict_and_frozen(filters):
    with pytest.raises(ValidationError):
        DashboardFilters.model_validate(filters)
    with pytest.raises(ValidationError):
        DashboardFilters().bucket = "month"


@pytest.mark.parametrize("bucket", ["day", "week", "month"])
def test_calendar_boundaries_are_preserved_without_bucket_overflow(bucket):
    saved = bundle(
        record("minimum", published="0001-01-01T00:00:00Z"),
        record("maximum", published="9999-12-31T23:59:59Z"),
        record("offset-overflow", published="0001-01-01T00:00:00+01:00"),
        record("future", published="2100-01-01T00:00:00Z"),
    )
    result = project_dashboard(saved, filters={"bucket": bucket})
    assert result.coverage.matching_records == 4
    assert result.coverage.unsupported_timestamp_records == 2
    assert result.coverage.unknown_timestamp_records == 2
    assert {rid for item in result.time_buckets for rid in item.record_ids} == {
        "minimum",
        "future",
    }
    assert {row.id: row.published_at for row in result.rows}["maximum"] == "9999-12-31T23:59:59Z"


def test_regular_saved_run_is_not_a_sentiment_run_and_missing_input_is_unknown():
    saved = annotated(bundle(record("one")))
    ordinary = project_dashboard(saved, filters={"sentiment_run_id": "saved"})
    assert ordinary.coverage.sentiment_run_available is False
    assert ordinary.rows[0].sentiment == "unknown"
    saved.runs[-1].input_manifest = []
    missing = project_dashboard(saved, filters={"sentiment_run_id": "sentiment-1"})
    assert missing.rows[0].sentiment == "unknown"
    assert missing.coverage.invalid_annotation_records == 1
